"""Read-only export of enrolled students' profiles — credential columns
never leave the database."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
import student_details_report as rpt


def test_credential_columns_are_stripped():
    cols = {"id", "name", "phone", "password_hash", "reset_token",
            "otp_code", "api_secret", "email", "division"}
    out = rpt.safe_columns(cols)
    assert "password_hash" not in out and "reset_token" not in out
    assert "otp_code" not in out and "api_secret" not in out
    assert "phone" in out and "division" in out and "email" in out


def test_the_tool_is_read_only():
    src = open(os.path.join(os.path.dirname(__file__), "..", "tools",
                            "student_details_report.py"), encoding="utf-8").read()
    for forbidden in ("UPDATE ", "INSERT ", "DELETE ", "REPLACE ", "DROP "):
        assert forbidden not in src, forbidden
