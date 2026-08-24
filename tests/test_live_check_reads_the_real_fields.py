"""live_check must read the fields the status route actually returns.

24 Aug 2026: live_check polled `body["progress"]["pending"]`. The route has
no "progress" object — total/done/skipped/failed/pending are top-level. So
every counter read as 0, "pending" fell back to its default of 1, and the
check reported

    FAIL  still not finished after 300s — the worker may be stuck

against a Space whose worker had done the review. A checker that fails when
the system works is worse than no checker: it sends you debugging the wrong
half of the stack at three in the morning.

These two tests lock the contract between the route and the tool.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# What the tool depends on being present in the status body.
NEEDED = {"total", "done", "skipped", "failed", "pending", "state"}


def _returned_keys(source: str, func: str) -> set:
    """String keys of the dict literal `func` returns."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func:
            for inner in ast.walk(node):
                if isinstance(inner, ast.Return) and isinstance(inner.value, ast.Dict):
                    return {k.value for k in inner.value.keys
                            if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    raise AssertionError(f"no dict-returning {func}() found")


def test_the_status_route_returns_every_field_live_check_reads():
    keys = _returned_keys(
        (ROOT / "app" / "routes" / "review_jobs.py").read_text(encoding="utf-8"),
        "job_status")
    missing = NEEDED - keys
    assert not missing, (
        f"GET /api/review/jobs/{{id}} no longer returns {sorted(missing)}; "
        f"tools/live_check.py reads them and will report a working Space as "
        f"stuck. Update both together.")


def test_live_check_does_not_look_for_a_nested_progress_object():
    """The exact mistake, named so it cannot come back quietly."""
    src = (ROOT / "tools" / "live_check.py").read_text(encoding="utf-8")
    assert 'body.get("progress")' not in src
    assert "body.get('progress')" not in src
    assert 'body.get("total", 0) > 0' in src, (
        "a job with zero items must not be reported as drained")
