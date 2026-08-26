"""The agent's storeOnly upsert shares assignment_submissions with the LMS
Coursework writer, which stores the upload URL FIRST. Before 26 Aug the
agent call carried no fileUrl and its plain VALUES(file_path) overwrote the
stored URL with NULL — the source of the daily-growing "text but no stored
file" count. COALESCE makes a NULL from any writer unable to erase a file.
"""
import re


def _sql():
    src = open("app/services/assignment_db_service.py", encoding="utf-8").read()
    m = re.search(r"INSERT INTO assignment_submissions.*?feedback\s*=\s*NULL",
                  src, re.S)
    assert m, "the save_assignment_submission upsert must exist"
    return m.group(0)


def test_null_file_path_cannot_erase_a_stored_one():
    sql = _sql()
    assert "COALESCE(VALUES(file_path), file_path)" in sql
    assert "COALESCE(VALUES(file_name), file_name)" in sql


def test_notes_still_replace_on_resubmit():
    # Only the file columns are null-protected; a resubmit must still be able
    # to replace the text and clear the stale grade.
    sql = _sql()
    assert "notes        = VALUES(notes)" in sql
    assert "grade        = NULL" in sql
