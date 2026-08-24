"""Turning the fallback OFF silently turned the retries off too.

24 Aug 02:35, mid-run, with PROVIDER_FALLBACK=off:

    503 - Provider capacity is temporarily unavailable. Please retry later.

The provider asked us to retry. We raised a 500 and the student's review
failed — on a blip that clears in seconds.

Cause: `tries = 1 + (GATEWAY_RETRIES if remaining else 0)`. Retries were
granted only to a provider with a fallback behind it, because they existed to
keep spend in the cheap seat, not to survive an outage. With one provider in
the chain, `remaining` is empty, so the configuration chosen to CONTROL COST
removed all resilience.

A sole provider needs retries more than a chained one, not less: there is
nowhere else to go.
"""

import pytest

from app.services import ai_service as ai


class _Boom(Exception):
    def __init__(self, status_code, message="Provider capacity is temporarily unavailable"):
        super().__init__(message)
        self.status_code = status_code


def _chain_of(n, monkeypatch):
    providers = [ai.Provider(f"p{i}", "key", "https://p.example",
                             bearer_auth=True, strict=False) for i in range(n)]
    monkeypatch.setattr(ai, "providers", lambda: providers)
    return providers


def _client_that(monkeypatch, outcomes):
    """A fake client whose calls raise/return from `outcomes` in order."""
    calls = []

    class _Messages:
        def create(self, **kwargs):
            calls.append(kwargs)
            result = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
            if isinstance(result, Exception):
                raise result
            return result

    monkeypatch.setattr(ai, "_client_for",
                        lambda p: type("C", (), {"messages": _Messages()})())
    return calls


# ── the defect ──────────────────────────────────────────────────────────────

def test_a_sole_provider_retries_a_503(monkeypatch):
    """The exact failure: one provider, a transient 503, no retry."""
    _chain_of(1, monkeypatch)
    calls = _client_that(monkeypatch, [_Boom(503), _Boom(503), "ok"])
    response, name = ai.create_message(_sleeper=lambda s: None, model="m")
    assert response == "ok" and name == "p0"
    assert len(calls) == 3, "it gave up instead of retrying"


def test_it_retries_the_same_provider_not_a_different_one(monkeypatch):
    """With no fallback there IS nowhere else — but the rule must hold even
    when a fallback exists: transient means wait, not switch."""
    _chain_of(2, monkeypatch)
    _client_that(monkeypatch, [_Boom(503), "ok"])
    _response, name = ai.create_message(_sleeper=lambda s: None, model="m")
    assert name == "p0", "a blip must not become direct-API spend"


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_every_transient_status_is_retried_on_a_sole_provider(status, monkeypatch):
    _chain_of(1, monkeypatch)
    calls = _client_that(monkeypatch, [_Boom(status), "ok"])
    ai.create_message(_sleeper=lambda s: None, model="m")
    assert len(calls) == 2, f"{status} was treated as permanent"


def test_it_gives_up_eventually_rather_than_looping(monkeypatch):
    """A wedged provider must not hold a review forever."""
    _chain_of(1, monkeypatch)
    calls = _client_that(monkeypatch, [_Boom(503)])
    with pytest.raises(Exception):
        ai.create_message(_sleeper=lambda s: None, model="m")
    assert len(calls) == 1 + ai.GATEWAY_RETRIES


def test_it_backs_off_between_attempts(monkeypatch):
    """Hammering a provider that just said 'capacity unavailable' is rude and
    counter-productive."""
    _chain_of(1, monkeypatch)
    _client_that(monkeypatch, [_Boom(503), _Boom(503), "ok"])
    waits = []
    ai.create_message(_sleeper=waits.append, model="m")
    assert waits == sorted(waits) and waits[0] > 0, waits


# ── permanent failures still fail fast ─────────────────────────────────────

@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_a_permanent_failure_is_not_retried(status, monkeypatch):
    """A bad key or a missing model cannot be cured by waiting, and burning
    four attempts on it delays the honest error."""
    _chain_of(1, monkeypatch)
    calls = _client_that(monkeypatch, [_Boom(status, "invalid api key")])
    with pytest.raises(Exception):
        ai.create_message(_sleeper=lambda s: None, model="m")
    assert len(calls) == 1


def test_a_first_attempt_that_works_costs_nothing_extra(monkeypatch):
    _chain_of(1, monkeypatch)
    calls = _client_that(monkeypatch, ["ok"])
    ai.create_message(_sleeper=lambda s: None, model="m")
    assert len(calls) == 1
