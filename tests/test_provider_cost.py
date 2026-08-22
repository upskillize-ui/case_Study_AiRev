"""Spend stays on the gateway. Ranjana, 22 Aug: "make sure cost should go
startup ai not Claude."

The 21-22 Aug sweeps showed the leak: startupapi 503s under batch load,
healthy again seconds later — but the chain fell through to the official API
on the FIRST failure, so a transient capacity blip billed reviews at direct
rates. Now a non-final provider's transient failures are retried with backoff
on the SAME provider before anything falls through, and PROVIDER_FALLBACK=off
removes the official API from the chain entirely while the gateway is
configured.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

from app.services import ai_service as ai


class _Err(Exception):
    def __init__(self, status):
        self.status_code = status
        super().__init__(f"HTTP {status}")


class _FakeClient:
    """Stands in for the SDK client; the script decides each call's fate."""

    def __init__(self, outcomes, calls, name):
        self._outcomes, self._calls, self._name = outcomes, calls, name
        self.messages = self

    def create(self, **kwargs):
        self._calls.append(self._name)
        fate = self._outcomes.pop(0)
        if isinstance(fate, Exception):
            raise fate
        return fate


def _wire(monkeypatch, gateway_outcomes, direct_outcomes):
    monkeypatch.setenv("STARTUPAPI_API_KEY", "gw-key")
    monkeypatch.setenv("STARTUPAPI_BASE_URL", "https://startupapi.io")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "direct-key")
    calls = []
    scripts = {"startupapi": list(gateway_outcomes),
               "anthropic": list(direct_outcomes)}
    monkeypatch.setattr(ai, "_client_for",
                        lambda p: _FakeClient(scripts[p.name], calls, p.name))
    return calls


def test_a_transient_503_is_waited_out_on_the_gateway(monkeypatch):
    """The exact sweep shape: 503, 503, then healthy. All three calls (and
    the money) stay on startupapi; the official API is never dialled."""
    calls = _wire(monkeypatch,
                  gateway_outcomes=[_Err(503), _Err(503), "OK"],
                  direct_outcomes=["NEVER"])
    naps = []
    response, served_by = ai.create_message(_sleeper=naps.append,
                                            model="m", messages=[])
    assert (response, served_by) == ("OK", "startupapi")
    assert calls == ["startupapi"] * 3
    assert naps == [ai.GATEWAY_RETRY_BASE * 1, ai.GATEWAY_RETRY_BASE * 2]


def test_a_hard_gateway_failure_falls_through_without_wasted_waits(monkeypatch):
    """A bad key (401) is not cured by waiting — one gateway attempt, then
    the safety net, loudly."""
    calls = _wire(monkeypatch,
                  gateway_outcomes=[_Err(401)],
                  direct_outcomes=["OK"])
    naps = []
    response, served_by = ai.create_message(_sleeper=naps.append,
                                            model="m", messages=[])
    assert (response, served_by) == ("OK", "anthropic")
    assert calls == ["startupapi", "anthropic"] and naps == []


def test_retries_exhausted_still_reach_the_safety_net(monkeypatch):
    """Persistent gateway trouble must not fail the learner's review while a
    working provider exists — fallback remains the default behaviour."""
    calls = _wire(monkeypatch,
                  gateway_outcomes=[_Err(503)] * (1 + ai.GATEWAY_RETRIES),
                  direct_outcomes=["OK"])
    response, served_by = ai.create_message(_sleeper=lambda s: None,
                                            model="m", messages=[])
    assert served_by == "anthropic"
    assert calls.count("startupapi") == 1 + ai.GATEWAY_RETRIES


def test_fallback_off_pins_every_rupee_to_the_gateway(monkeypatch):
    """PROVIDER_FALLBACK=off: with the gateway configured, the official API
    is not in the chain at all — a dead gateway is a FAIL to re-run later,
    never silent direct spend."""
    monkeypatch.setenv("PROVIDER_FALLBACK", "off")
    calls = _wire(monkeypatch,
                  gateway_outcomes=[_Err(503)] * (1 + ai.GATEWAY_RETRIES),
                  direct_outcomes=["NEVER"])
    assert [p.name for p in ai.providers()] == ["startupapi"]
    with pytest.raises(_Err):
        ai.create_message(_sleeper=lambda s: None, model="m", messages=[])
    assert "anthropic" not in calls


def test_fallback_off_without_a_gateway_never_means_no_claude(monkeypatch):
    """The switch pins spend; it must never disable reviewing outright."""
    monkeypatch.setenv("PROVIDER_FALLBACK", "off")
    monkeypatch.delenv("STARTUPAPI_API_KEY", raising=False)
    monkeypatch.delenv("STARTUPAPI_BASE_URL", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "direct-key")
    assert [p.name for p in ai.providers()] == ["anthropic"]


def test_the_final_provider_gets_no_retry_loop(monkeypatch):
    """Retries exist to keep spend in the cheap seat. The last link keeps its
    single attempt — its caller owns any further fallback (HuggingFace)."""
    monkeypatch.delenv("STARTUPAPI_API_KEY", raising=False)
    monkeypatch.delenv("STARTUPAPI_BASE_URL", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "direct-key")
    calls = []
    monkeypatch.setattr(ai, "_client_for",
                        lambda p: _FakeClient([_Err(503)], calls, p.name))
    with pytest.raises(_Err):
        ai.create_message(_sleeper=lambda s: None, model="m", messages=[])
    assert calls == ["anthropic"]


def test_transient_classifier_is_pure_and_exact():
    assert ai._gateway_transient(_Err(503)) is True
    assert ai._gateway_transient(_Err(429)) is True
    assert ai._gateway_transient(Exception("timeout, no status")) is True
    assert ai._gateway_transient(_Err(401)) is False
    assert ai._gateway_transient(_Err(404)) is False
    assert ai._gateway_transient(_Err(400)) is False
