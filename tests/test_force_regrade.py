"""--force, and why it exists.

23 Aug: every Day 07 re-review was refused with "content shrank 186 -> 62
words — row untouched". The guard was right about the facts and wrong about
this case: the text HAD shrunk, because the Gemini shell that used to supply
120 words of Google's own page is now refused. The shrink is the fix working,
and the marks being protected were scored on that shell.

So the override is a flag, not a default — the guard is correct every other
time — and it must travel as a QUERY parameter, because that is what the route
reads. Sent in the body it is silently ignored and the whole run does nothing.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import inspect

import bulk_review


def _regrade_block() -> str:
    src = inspect.getsource(bulk_review.review_one)
    return src[src.index("if redo:"):src.index("headers = {")]


def test_force_travels_as_a_query_parameter_not_a_body_field():
    """The route declares force as a query param. In the body it is ignored
    and the run silently does nothing."""
    block = _regrade_block()
    assert 'params.append("force=true")' in block
    assert '"force": True' not in block, \
        "a body field here would be dropped by FastAPI without an error"


def test_dry_run_and_force_can_be_sent_together():
    block = _regrade_block()
    assert '"&".join(params)' in block


def test_the_route_really_reads_them_from_the_query():
    """Proves the client and the server agree, rather than assuming it."""
    from app.routes.assignment_review import re_review_assignment
    params = inspect.signature(re_review_assignment).parameters
    assert params["force"].annotation is bool
    assert params["dryRun"].annotation is bool


def test_force_is_off_unless_asked_for():
    assert inspect.signature(bulk_review.review_one).parameters["force"].default is False


def test_the_flag_is_exposed_and_explains_itself():
    src = inspect.getsource(bulk_review)
    assert '"--force", action="store_true"' in src
    helptext = src[src.index('"--force"'):src.index('"--no-abort"')]
    assert "SHORTER" in helptext, "the flag must say what it overrides"


def test_the_shrink_guard_still_protects_an_ordinary_rerun():
    """Without --force the guard must still fire — it is right every time
    except the one migration it was blocking."""
    from app.routes.assignment_review import content_shrunk
    assert content_shrunk(186, 62) is True
    assert content_shrunk(186, 180) is False
