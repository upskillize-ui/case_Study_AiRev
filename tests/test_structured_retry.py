"""Three times on the morning of 19 Aug a review died with

    Exception: Model returned no structured result (model=claude-haiku-4-5)

riding up to the route as a bare 500 — once killing the very first canary
regrade of submission 4832. An immediate, byte-identical re-send succeeded,
which is the whole diagnosis: the model occasionally answers with neither a
tool call nor parseable text, and the code treated a stochastic hiccup as a
terminal failure.

call_structured now retries exactly once. On the deterministic (non-thinking)
path the retry also forces the tool call — tool_choice {"type": "tool"} is
only incompatible with extended thinking, which that path does not use. The
thinking path retries with choice "auto" unchanged.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

from app.services import ai_service as ais


class _ToolBlock:
    type, name, input = "tool_use", "emit_result", {"ok": True}


class _GoodResp:
    content = [_ToolBlock()]
    usage = None


class _EmptyResp:
    """Neither a tool call nor any text — the live failure shape."""
    content = []
    usage = None


def _wire(monkeypatch, responses):
    """create_message pops from `responses`; returns the list of kwargs seen."""
    calls = []

    def fake_create_message(**kw):
        calls.append(dict(kw))
        return responses.pop(0), "anthropic"

    monkeypatch.setattr(ais, "create_message", fake_create_message)
    monkeypatch.setattr(ais, "_report_usage", lambda *a, **k: None)
    monkeypatch.setattr(ais, "_first_text", lambda r: "")
    return calls


def test_one_empty_response_is_survived(monkeypatch):
    """The live incident replayed: first answer empty, retry succeeds, the
    caller never knows."""
    calls = _wire(monkeypatch, [_EmptyResp(), _GoodResp()])
    result = ais.call_structured(blocks=[{"text": "x"}], schema={})
    assert result == {"ok": True}
    assert len(calls) == 2


def test_the_retry_forces_the_tool_call_on_the_deterministic_path(monkeypatch):
    """Asking nicely failed once already — the second attempt must not repeat
    the exact conditions of the first."""
    calls = _wire(monkeypatch, [_EmptyResp(), _GoodResp()])
    ais.call_structured(blocks=[{"text": "x"}], schema={})
    assert calls[0]["tool_choice"] == {"type": "auto"}
    assert calls[1]["tool_choice"] == {"type": "tool", "name": "emit_result"}


def test_the_thinking_path_never_forces(monkeypatch):
    """Forced tool_choice is incompatible with extended thinking — forcing on
    the escalation retry would trade a transient failure for a guaranteed
    API error."""
    calls = _wire(monkeypatch, [_EmptyResp(), _GoodResp()])
    ais.call_structured(blocks=[{"text": "x"}], schema={}, thinking_budget=1000)
    assert calls[1]["tool_choice"] == {"type": "auto"}


def test_two_empty_responses_still_raise(monkeypatch):
    """One retry, not an infinite loop. A model that fails twice is genuinely
    down, and the caller's own fallback (503, row untouched) must engage."""
    calls = _wire(monkeypatch, [_EmptyResp(), _EmptyResp()])
    with pytest.raises(Exception, match="no structured result"):
        ais.call_structured(blocks=[{"text": "x"}], schema={})
    assert len(calls) == 2


def test_a_first_try_success_makes_exactly_one_call(monkeypatch):
    """The retry must cost nothing on the 99% path."""
    calls = _wire(monkeypatch, [_GoodResp()])
    result = ais.call_structured(blocks=[{"text": "x"}], schema={})
    assert result == {"ok": True}
    assert len(calls) == 1
