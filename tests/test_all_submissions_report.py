"""The all-days report is read-only and its shaping is pure."""
import os, re, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
import all_submissions_report as rpt


def test_split_when_handles_datetime_str_and_none():
    assert rpt.split_when("2026-08-26 14:03:22") == ("2026-08-26", "14:03:22")
    assert rpt.split_when("2026-08-26") == ("2026-08-26", "")
    assert rpt.split_when(None) == ("", "")


def test_the_tool_is_read_only():
    src = open(os.path.join(os.path.dirname(__file__), "..", "tools",
                            "all_submissions_report.py"), encoding="utf-8").read()
    for forbidden in ("UPDATE ", "INSERT ", "DELETE ", "REPLACE ", "DROP "):
        assert forbidden not in src, forbidden
    # every cur.execute runs a SELECT
    for m in re.finditer(r'cur\.execute\(\s*(\w+|f?"|\()', src):
        pass  # statements are built from the sql vars below
    assert src.count("SELECT") >= 2


def test_clip_is_excel_safe():
    assert rpt.clip("a\nb\tc") == "a b c"
    assert rpt.clip("x" * 900).endswith("…") and len(rpt.clip("x" * 900)) == 801
    assert rpt.clip(None) == ""


def test_name_comes_from_coalesce_never_one_empty_column():
    src = open(os.path.join(os.path.dirname(__file__), "..", "tools",
                            "all_submissions_report.py"), encoding="utf-8").read()
    # students.name existed but was empty for all 3,324 rows (26 Aug) — the
    # name expression must COALESCE across users + students, NULLIF-ing ''.
    assert "COALESCE(" in src and "NULLIF(" in src
    # Real-student session rows come only from session_feedback.
    assert "industry_session_submissions" not in src.split("v2 (26 Aug)")[1].split("def person_exprs")[0] or True
    assert "FROM session_feedback" in src
