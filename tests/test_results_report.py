"""The full-roster CSV: every student, graded or not, with the reason column
carrying the agent's own stamped note for problem rows.

The identity layer is exercised against a fake connection, because the real
one lied: 22 Aug the run died on `st.user_id`, a column information_schema
listed and the table did not have. Everything here proves the report now
survives that — and every other schema surprise — with a blank cell."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

from results_report import (feedback_fields, reason_for, resolve_identities,
                            shape, usable_columns)


def test_a_graded_row_shows_band_ai_and_coaching_text():
    fb = feedback_fields(json.dumps({
        "grade": "B+", "aiLikelihoodPercent": 45,
        "summary": "Solid research, thin sourcing.",
        "feedbackPoints": ["Name your sources.", "Add the RBI rules.", "x"],
        "hardTruth": "Research without sources is opinion."}))
    assert fb["band"] == "B+" and fb["ai_percent"] == 45
    assert "Solid research" in fb["feedback"]
    assert "Name your sources." in fb["feedback"]
    assert "Bottom line: Research without sources" in fb["feedback"]


def test_a_stamped_not_graded_note_becomes_the_reason(monkeypatch=None):
    fb = feedback_fields(json.dumps({
        "notGraded": True, "rubricScores": [],
        "message": "Not graded: your file looks like a study guide, not this "
                   "assignment's work. Please attach the correct work."}))
    row = {"grade": None, "notes_len": 200, "file_ref": "x.pdf"}
    assert "study guide" in reason_for(row, fb)


def test_an_empty_row_gets_the_resubmit_reason():
    row = {"grade": None, "notes_len": 0, "file_ref": ""}
    assert "submit your work again" in reason_for(row, feedback_fields(None))


def test_an_unprocessed_row_is_no_action_needed():
    """Ungraded, has content, no stamped note: the batch simply hasn't
    reached it — never tell this student to act."""
    row = {"grade": None, "notes_len": 500, "file_ref": ""}
    assert "No action needed" in reason_for(row, feedback_fields("{}"))


def test_graded_rows_never_get_a_reason():
    row = {"grade": 7.5, "notes_len": 0, "file_ref": ""}
    assert reason_for(row, feedback_fields("{}")) == ""


def test_malformed_feedback_never_crashes_the_roster():
    for bad in (None, "", "not json {", 42, "[1,2]"):
        fb = feedback_fields(bad)
        assert fb["feedback"] == "" and fb["band"] == ""


def test_shape_puts_top_marks_first_then_problem_rows():
    rows = [{"grade": None, "student_name": "b"},
            {"grade": 9.1, "student_name": "a"},
            {"grade": 3.0, "student_name": "c"},
            {"grade": None, "student_name": "a"}]
    out = shape(rows)
    assert [r.get("grade") for r in out] == [9.1, 3.0, None, None]
    assert out[2]["student_name"] == "a"        # ungraded alphabetical


# ── the identity layer: probe, never trust ─────────────────────────────────

class FakeCursor:
    def __init__(self, db):
        self.db, self.rows = db, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        self.db["log"].append(sql)
        low = sql.lower()
        table = "students" if "`students`" in low else "users"
        if table not in self.db:
            raise RuntimeError("1146 table does not exist")
        cols = [c.strip("`") for c in
                sql.split("SELECT ")[1].split(" FROM")[0].split(", ")]
        real = self.db[table]
        for c in cols:
            if c not in self.db[f"{table}_cols"]:
                raise RuntimeError(f'1054 Unknown column {c}')
        if "LIMIT 0" in sql:
            self.rows = []
            return
        wanted = set(params)
        self.rows = [{c: r.get(c) for c in cols}
                     for r in real if r["id"] in wanted]

    def fetchall(self):
        return self.rows


class FakeConn:
    def __init__(self, db):
        self.db = db

    def cursor(self):
        return FakeCursor(self.db)


def _db(**kw):
    base = {"log": [],
            "students_cols": {"id", "name", "email"},
            "students": [{"id": 1, "name": "Asha", "email": "a@x.com"},
                         {"id": 2, "name": "Ravi", "email": "r@x.com"}]}
    base.update(kw)
    return base


def test_a_column_information_schema_invented_is_never_selected():
    """`user_id` is listed as a candidate but does not exist — the probe must
    reject it, and no later query may mention it."""
    db = _db()
    conn = FakeConn(db)
    assert usable_columns(conn, "students", ("user_id",)) == []
    who = resolve_identities(conn, [1, 2])
    assert who[1] == {"name": "Asha", "email": "a@x.com"}
    assert who[2]["email"] == "r@x.com"
    assert not any("user_id" in q for q in db["log"] if "LIMIT 0" not in q)


def test_email_arrives_from_users_when_students_has_no_email_column():
    db = _db(students_cols={"id", "name", "user_id"},
             students=[{"id": 1, "name": "Asha", "user_id": 91}],
             users_cols={"id", "email"},
             users=[{"id": 91, "email": "asha@x.com"}])
    who = resolve_identities(FakeConn(db), [1])
    assert who[1] == {"name": "Asha", "email": "asha@x.com", "user_id": 91}


def test_ids_share_a_space_when_there_is_no_link_column():
    db = _db(students_cols={"id", "name"},
             students=[{"id": 7, "name": "Meera"}],
             users_cols={"id", "email"},
             users=[{"id": 7, "email": "meera@x.com"}])
    who = resolve_identities(FakeConn(db), [7])
    assert who[7]["name"] == "Meera" and who[7]["email"] == "meera@x.com"


def test_no_students_table_at_all_still_returns_a_row_per_student():
    db = {"log": [], "users_cols": {"id", "name"},
          "users": [{"id": 5, "name": "Kiran"}]}
    who = resolve_identities(FakeConn(db), [5, 6])
    assert who[5]["name"] == "Kiran" and who[5]["email"] == ""
    assert who[6] == {"name": "", "email": ""}      # unknown, never missing


def test_an_empty_roster_asks_the_database_nothing():
    db = _db()
    assert resolve_identities(FakeConn(db), []) == {}
    assert db["log"] == []
