"""A truncated answer is not an answer (04 Sep 2026, jobs 849-857 live).

When the model's output hit max_tokens mid tool-call, the API still returned
a tool_use block with an EMPTY input. call_structured returned that {} as the
review; the guard refused it ("produced no feedback at all"); the row was
filed as our side and re-offered — and the same long submission truncated at
the same point on every retry. Same five ids, sweep after sweep.

Pinned: a max_tokens stop is retried once with double the room, and an empty
tool input is never returned as a result.
"""
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

from app.services import ai_service as ais


class _Tool:
    type = "tool_use"
    name = "emit_result"

    def __init__(self, payload):
        self.input = payload


class _Resp:
    usage = None

    def __init__(self, stop_reason, payload):
        self.stop_reason = stop_reason
        self.content = [_Tool(payload)]


def _run(monkeypatch, responses):
    calls = []

    def fake(**kw):
        calls.append(dict(kw))
        return responses[len(calls) - 1], "anthropic"

    monkeypatch.setattr(ais, "create_message", fake)
    monkeypatch.setattr(ais, "_report_usage", lambda *a, **k: None)
    out = ais.call_structured(blocks=[{"text": "x", "cache": False}], schema={},
                              max_tokens=3500)
    return out, calls


def test_a_truncated_answer_is_retried_with_double_the_room(monkeypatch):
    out, calls = _run(monkeypatch, [
        _Resp("max_tokens", {}),                      # ran out mid tool-call
        _Resp("tool_use", {"criteria": [{"name": "a"}]})])
    assert out == {"criteria": [{"name": "a"}]}
    assert len(calls) == 2
    assert calls[0]["max_tokens"] == 3500 and calls[1]["max_tokens"] == 7000


def test_a_truncated_answer_with_partial_input_is_still_not_returned(monkeypatch):
    """Whatever the API managed to fill in before the cut is not the review."""
    out, calls = _run(monkeypatch, [
        _Resp("max_tokens", {"criteria": [{"name": "half"}]}),
        _Resp("end_turn", {"criteria": [{"name": "whole"}]})])
    assert out == {"criteria": [{"name": "whole"}]}
    assert len(calls) == 2


def test_two_truncations_raise_a_transport_style_error(monkeypatch):
    with pytest.raises(Exception) as e:
        _run(monkeypatch, [_Resp("max_tokens", {}), _Resp("max_tokens", {})])
    assert "no structured result" in str(e.value)
    assert "truncated" in str(e.value)


def test_a_complete_answer_costs_one_call(monkeypatch):
    out, calls = _run(monkeypatch, [_Resp("end_turn", {"criteria": []})])
    assert out == {"criteria": []} and len(calls) == 1
