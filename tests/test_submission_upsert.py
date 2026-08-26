"""The production DB now enforces UNIQUE KEY uq_submission
(assignment_id, student_id) on assignment_submissions — added 18 Aug to close
the LMS duplicate-row race. The same index turned AiRev's own submit path into
a crash: save_assignment_submission INSERTed a fresh row per attempt, so every
resubmission through AiRev's UI died with

    pymysql.err.IntegrityError: (1062, "Duplicate entry '17-731'
        for key 'assignment_submissions.uq_submission'")

— seen live for submissions 17-731, 25-192 and 26-1308 while students clicked
Submit and got a 500.

The fix mirrors the LMS Coursework handler: one atomic upsert, one row per
(assignment, student), stale grade/feedback cleared so a new review never sits
beside an old score. These tests pin the statement's load-bearing parts with a
fake texecute — no database, because what matters is the SQL's shape, and a
1062 in production already demonstrated what the wrong shape does.
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

from app.services import assignment_db_service as svc


class _FakeTenant:
    id = "lms"
    database_url = "mysql://fake"


def _capture(monkeypatch, lastrowid=4321):
    """Replace texecute/tquery with recorders. Returns the shared call log."""
    calls = []

    def fake_texecute(tenant, sql, params=()):
        calls.append(("texecute", sql, params))
        return lastrowid

    def fake_tquery(tenant, sql, params=()):
        calls.append(("tquery", sql, params))
        return []

    monkeypatch.setattr(svc, "texecute", fake_texecute)
    monkeypatch.setattr(svc, "tquery", fake_tquery)
    return calls


def _save(monkeypatch, **kw):
    calls = _capture(monkeypatch, **kw)
    result = svc.save_assignment_submission(
        _FakeTenant(), 17, 731, "my five-year plan", "/uploads/plan.png", "plan.png")
    return calls, result


def test_a_resubmission_is_one_statement_not_insert_then_crash(monkeypatch):
    """One atomic upsert. No separate INSERT that can hit uq_submission, no
    read-modify-write race, and no follow-up COUNT query — a single texecute
    is the entire write path."""
    calls, _ = _save(monkeypatch)
    assert len(calls) == 1, [c[0] for c in calls]
    kind, sql, params = calls[0]
    assert kind == "texecute"
    assert "ON DUPLICATE KEY UPDATE" in sql
    assert sql.count("INSERT INTO") == 1
    assert params[0] == 17 and params[1] == 731


def test_the_updated_row_id_comes_back_not_a_meaningless_lastrowid(monkeypatch):
    """On the UPDATE branch of an upsert, cursor.lastrowid is garbage unless
    the statement claims the existing id via LAST_INSERT_ID(id). The pipeline
    writes the AI review into whatever id this returns — without the trick a
    resubmit would grade the wrong row."""
    calls, result = _save(monkeypatch, lastrowid=99)
    _, sql, _ = calls[0]
    assert re.search(r"id\s*=\s*LAST_INSERT_ID\(id\)", sql), sql
    assert result["submissionId"] == 99


def test_a_resubmit_clears_the_stale_grade_and_feedback(monkeypatch):
    """New work must not sit under an old score. The LMS handler resets
    grade/feedback on resubmit; AiRev's writer now does the same, so both
    writers keep one shape on the shared table."""
    calls, _ = _save(monkeypatch)
    _, sql, _ = calls[0]
    update_clause = sql.split("ON DUPLICATE KEY UPDATE", 1)[1]
    assert re.search(r"grade\s*=\s*NULL", update_clause), update_clause
    assert re.search(r"feedback\s*=\s*NULL", update_clause), update_clause
    assert re.search(r"status\s*=\s*'submitted'", update_clause)
    assert re.search(r"submitted_at\s*=\s*NOW\(\)", update_clause)


def test_the_new_content_replaces_the_old_on_every_column_we_write(monkeypatch):
    """A resubmit WITH a new file still replaces last week's upload (COALESCE
    lets a non-NULL value win). But a NULL must no longer erase a stored file:
    the LMS writer stores the upload URL first and the agent's storeOnly call
    historically carried no fileUrl — plain VALUES() wiped the URL seconds
    after it was saved, growing the "text but no stored file" count daily."""
    calls, _ = _save(monkeypatch)
    _, sql, _ = calls[0]
    update_clause = sql.split("ON DUPLICATE KEY UPDATE", 1)[1]
    assert re.search(r"notes\s*=\s*VALUES\(notes\)", update_clause)
    for col in ("file_path", "file_name"):
        assert f"COALESCE(VALUES({col}), {col})" in update_clause, col


def test_none_answer_text_is_stored_as_empty_string_not_null(monkeypatch):
    """notes is NOT NULL in some tenant schemas; the old code coerced None
    to '' and the upsert must keep doing it."""
    calls = _capture(monkeypatch)
    svc.save_assignment_submission(_FakeTenant(), 17, 731, None, None, None)
    _, _, params = calls[0]
    assert params[2] == ""
