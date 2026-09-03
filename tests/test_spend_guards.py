# tests/test_spend_guards.py
# ---------------------------------------------------------------------------
# THE RULES THAT STOP MONEY BEING SPENT TWICE ON THE SAME ANSWER (03 Sep 2026).
#
#   1. A refusal is re-offered only when the thing that caused it might have
#      changed. Our failures retry on a rules change; a verdict about the
#      submission waits for the learner.
#   2. A tenant whose last sweep aborted for a dead provider cools off.
#   3. A file is read once. The intake cache returns the same artefacts for the
#      same input and misses the moment the learner changes anything.
#   4. Rejected consensus anchors are not re-bought nightly.
#
# The phrase tests read the SOURCE of the modules that write each refusal, so a
# reworded message fails here before it can silently turn a retryable failure
# into a permanent one in production.
#
# Pure + fake DB. No network, no model, no real database.
# ---------------------------------------------------------------------------

import io
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)
sys.path.insert(0, _HERE)

from _stubs import import_with_stubs

sweeper = import_with_stubs("app.services.sweeper_service")
cache = import_with_stubs("app.services.intake_cache")
from app.utils.submission_intake import Artefact

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} — {detail}")
        FAILURES.append(name)


def _src(rel):
    return io.open(os.path.join(_ROOT, rel), encoding="utf-8").read()


# --- 1. our refusal phrases are the ones actually written ---------------------

def test_our_refusal_phrases_match_their_writers():
    print("\nthe sweep's idea of OUR refusal matches what we actually write")
    guard_writer = _src("app/services/assignment_db_service.py")
    route_writer = _src("app/routes/assignment_review.py")
    check("grade guard (db service) phrase is real",
          "could not finish reviewing this attempt" in guard_writer)
    check("grade guard (route envelope) phrase is real",
          "could not complete a fair review" in route_writer)
    check("rubric-unavailable phrase is real",
          "could not read this task" in route_writer)
    for ph in sweeper.OUR_REFUSAL_PHRASES:
        check(f"'{ph}' is written by us somewhere",
              ph in guard_writer or ph in route_writer)
    # And the STUDENT-side messages must NOT match, or they would be retried.
    for theirs in ["could not open your file", "Not graded: what reached us looks like",
                   "would not open for us", "the link returned a web page"]:
        check(f"student-side '{theirs[:30]}…' is not one of ours",
              not any(p in theirs for p in sweeper.OUR_REFUSAL_PHRASES))


class SQLDB:
    def __init__(self, rows=None):
        self.rows, self.sql, self.params, self.queries = rows or [], "", (), []

    def __call__(self, tenant, sql, params=()):
        self.queries.append((sql, params))
        if "review_job_items" in sql:
            return []
        self.sql, self.params = sql, params
        return self.rows


def test_the_escape_arm_is_conditional_on_ours():
    print("\nthe rules-version escape opens only for our refusals")
    db = SQLDB()
    orig = sweeper.tquery; sweeper.tquery = db
    try:
        sweeper.find_unreviewed("lms")
    finally:
        sweeper.tquery = orig
    sql = db.sql
    check("escape arm requires the stamp to be missing AND the refusal to be ours",
          "NOT LIKE %s" in sql and "AND (COALESCE(s.feedback, '') LIKE %s" in sql, sql)
    check("one placeholder per phrase (stamp + ours + api-error)",
          sql.count("LIKE %s") == 1 + len(sweeper.OUR_REFUSAL_PHRASES) + len(sweeper.API_ERROR_PHRASES),
          sql.count("LIKE %s"))
    check("a refusal quoting a provider error is re-offered whatever its stamp",
          all(f"%{p}%" in db.params for p in sweeper.API_ERROR_PHRASES), db.params)
    check("phrases are bound as values with wildcards",
          all(f"%{p}%" in db.params for p in sweeper.OUR_REFUSAL_PHRASES), db.params)
    check("returned rows are excluded outright",
          "NOT IN ('draft', 'returned')" in sql)


# --- 2. cool-off ------------------------------------------------------------

def test_cool_off_after_an_aborted_sweep():
    print("\na tenant whose last sweep aborted waits before trying again")

    def db_with(last_state, recent):
        def q(tenant, sql, params=()):
            if "ORDER BY id DESC LIMIT 1" in sql and "updated_at" not in sql:
                return [{"state": last_state}]
            if "updated_at >=" in sql:
                return [{"hit": 1}] if recent else []
            return []
        return q
    orig = sweeper.tquery
    try:
        sweeper.tquery = db_with("aborted", True)
        check("aborted recently -> cooling off", sweeper.cooling_off("lms") is True)
        sweeper.tquery = db_with("aborted", False)
        check("aborted long ago -> sweep again", sweeper.cooling_off("lms") is False)
        sweeper.tquery = db_with("done", True)
        check("last sweep finished -> not cooling off", sweeper.cooling_off("lms") is False)
        def boom(*a, **k): raise RuntimeError("db down")
        sweeper.tquery = boom
        check("an unreadable jobs table fails OPEN (sweep runs)", sweeper.cooling_off("lms") is False)
    finally:
        sweeper.tquery = orig


# --- 3. intake cache --------------------------------------------------------

class CacheDB:
    def __init__(self):
        self.store = {}
        self.writes = 0

    def query(self, tenant, sql, params=()):
        if "SELECT fingerprint, artefacts" in sql:
            row = self.store.get(params[0])
            return [row] if row else []
        return []

    def execute(self, tenant, sql, params=()):
        if sql.strip().startswith("CREATE"):
            return 0
        if "INSERT INTO" in sql:
            sid, fp, blob, items, nbytes = params
            self.store[sid] = {"fingerprint": fp, "artefacts": blob}
            self.writes += 1
            return 1
        if "DELETE" in sql:
            self.store.pop(params[0], None)
            return 1
        return 0


def _with_cache_db(fn):
    db = CacheDB()
    oq, oe = cache.tquery, cache.texecute
    cache.tquery, cache.texecute = db.query, db.execute
    cache._tables_ready.clear()
    try:
        return fn(db)
    finally:
        cache.tquery, cache.texecute = oq, oe


def test_fingerprint_moves_with_what_the_learner_controls():
    print("\nthe fingerprint")
    a = cache.fingerprint("my answer", "https://x/y.pdf", "y.pdf")
    check("stable for the same input", a == cache.fingerprint("my answer", "https://x/y.pdf", "y.pdf"))
    check("changes when the notes change", a != cache.fingerprint("my answer edited", "https://x/y.pdf", "y.pdf"))
    check("changes when the file changes", a != cache.fingerprint("my answer", "https://x/z.pdf", "y.pdf"))
    check("whitespace-only edits do not count", a == cache.fingerprint("  my answer \n", "https://x/y.pdf ", "y.pdf"))
    check("changes when INTAKE_VERSION changes",
          a != __import__("hashlib").sha256("\x1f".join([f"v{cache.INTAKE_VERSION + 1}", "my answer", "https://x/y.pdf", "y.pdf"]).encode()).hexdigest())
    check("is a sha256 hex", len(a) == 64 and all(c in "0123456789abcdef" for c in a))


def test_read_once_ever():
    print("\nread once, ever")
    arts = [Artefact(kind="video", label="day17.mp4", text="WHAT IS SAID: hello",
                     image_b64="AAAA", media_type="image/jpeg", extra_images=["BBBB"]),
            Artefact(kind="typed text", label="typed", text="I made this")]

    def run(db):
        fp = cache.fingerprint("I made this", "https://res.cloudinary.com/x/day17.mp4", "day17.mp4")
        check("cold cache misses", cache.recall("lms", 4021, fp) is None)
        check("remember writes", cache.remember("lms", 4021, fp, arts) is True)
        back = cache.recall("lms", 4021, fp)
        check("warm cache hits", back is not None)
        check("same number of artefacts", back and len(back) == 2)
        check("text survives", back and back[0].text == "WHAT IS SAID: hello")
        check("the PICTURE survives — a cached re-review is not blind",
              back and back[0].image_b64 == "AAAA" and back[0].media_type == "image/jpeg")
        check("extra frames survive", back and back[0].extra_images == ["BBBB"])
        check("readable property still works", back and back[1].readable)
        other = cache.fingerprint("I made this, and fixed it", "https://res.cloudinary.com/x/day17.mp4", "day17.mp4")
        check("a resubmit (new notes) misses", cache.recall("lms", 4021, other) is None)
        check("one write so far", db.writes == 1)
        cache.forget("lms", 4021)
        check("forget drops it", cache.recall("lms", 4021, fp) is None)
    _with_cache_db(run)


def test_nothing_readable_is_never_cached():
    print("\na failed read is never made permanent")
    dead = [Artefact(kind="link", label="https://canva.com/x", note="link returned HTTP 403", confirmed=True)]
    def run(db):
        fp = cache.fingerprint("", "https://canva.com/x", "Link submission")
        check("remember refuses a row with nothing read",
              cache.remember("lms", 1, fp, dead) is False)
        check("nothing written", db.writes == 0)
        check("remember refuses an empty list", cache.remember("lms", 1, fp, []) is False)
        # A typed line beside an unopened link is NOT a complete read. Caching
        # it served the unopened link back on every re-review (04 Sep).
        partial = dead + [Artefact(kind="typed text", label="typed answer",
                                   text="here is my design https://canva.com/x")]
        check("remember refuses a partial read (one item unread)",
              cache.remember("lms", 1, fp, partial) is False)
        check("still nothing written", db.writes == 0)
    _with_cache_db(run)


def test_cache_never_raises():
    print("\nthe cache can fail; a review cannot")
    def boom(*a, **k): raise RuntimeError("db down")
    oq, oe = cache.tquery, cache.texecute
    cache.tquery, cache.texecute = boom, boom
    cache._tables_ready.clear()
    try:
        check("recall on a dead DB returns None", cache.recall("lms", 1, "x") is None)
        check("remember on a dead DB returns False",
              cache.remember("lms", 1, "x", [Artefact(kind="typed text", label="t", text="hi")]) is False)
        try:
            cache.forget("lms", 1); check("forget on a dead DB does not raise", True)
        except Exception:
            check("forget on a dead DB does not raise", False)
    finally:
        cache.tquery, cache.texecute = oq, oe


# --- 4. anchors -------------------------------------------------------------

def test_rejected_anchors_are_not_rebought_nightly():
    print("\nrejected consensus anchors wait before being retried")
    src = _src("app/services/consolidation_service.py")
    check("a rejection is remembered", "_ANCHOR_REJECTED" in src and "INSERT INTO calibration_notes" in src)
    check("…and checked before spending", "_recently_rejected(scope_type, scope_id)" in src)
    check("the memo is inactive (not a marker note)", "VALUES (%s,%s,%s,%s,0)" in src)
    check("the wait is configurable", "ANCHOR_RETRY_DAYS" in src)


# --- 5. our outage never blames the learner ------------------------------------

def test_a_billing_cap_is_our_outage():
    print("\nour account running out is ours")
    gg = import_with_stubs("app.services.grade_guard")
    live = ("OCR failed on anthropic: BadRequestError: Error code: 400 - {'type': 'error', "
            "'error': {'type': 'invalid_request_error', 'message': 'You have reached your "
            "specified API usage limits. You will regain access on 2026-10-01 at 00:00 UTC.'}, "
            "'request_id': 'req_011CegWG3EY1Qx7yeNBQRK4m'}")
    check("the exact 03 Sep message is ours", gg.reads_as_our_outage(live))
    for t in ["OCR failed on startupapi: Error code: 503",
              "transcription failed (APIConnectionError)",
              "could not process this recording (RateLimitError)",
              "usage limit reached", "spend limit exceeded", "insufficient credit balance",
              "request_id: req_x", "Error code: 529 overloaded"]:
        check(f"'{t[:38]}' is ours", gg.reads_as_our_outage(t))
    for t in ["password-protected PDF", "the file is empty", "zip contained no readable files",
              "no stored work found", "image too small to read"]:
        check(f"'{t}' is the learner's", not gg.reads_as_our_outage(t))


def test_machinery_never_reaches_a_card():
    print("\nwhat a learner is told")
    gg = import_with_stubs("app.services.grade_guard")
    raw = "OCR failed on anthropic: BadRequestError: Error code: 400 - {'type': 'error'} request_id: req_1"
    shown = gg.learner_facing(raw)
    check("an API error becomes a plain sentence", shown == "the file could not be read", shown)
    check("no request id leaks", "req_" not in shown)
    check("no exception class leaks", "Error" not in shown)
    check("a clean reason passes through", gg.learner_facing("password-protected PDF") == "password-protected PDF")
    check("an empty reason gets a sentence", gg.learner_facing("") == "the file could not be read")
    long = "x" * 300
    check("a long reason is cut for the card", len(gg.learner_facing(long)) <= 120)
    src = _src("app/routes/assignment_review.py")
    check("the regrade route uses it", "learner_facing(why)" in src)
    check("the submit route uses it", "learner_facing(file_error)" in src)


def test_outage_attempts_do_not_spend_the_ceiling():
    print("\na quota cap does not use up the learner's tries")
    # One real row, or attempts_spent() short-circuits before it queries.
    db = SQLDB(rows=[{"id": 9, "assignment_id": 18, "student_id": 1, "submitted_at": "2026-09-03"}])
    orig = sweeper.tquery; sweeper.tquery = db
    try:
        sweeper.find_unreviewed("lms")
    finally:
        sweeper.tquery = orig
    ledger = [q for q, _ in db.queries if "review_job_items" in q][0]
    check("our-outage items are excluded from the attempt count",
          "detail NOT LIKE 'our outage %'" in ledger, ledger)


for fn in [test_our_refusal_phrases_match_their_writers,
           test_the_escape_arm_is_conditional_on_ours,
           test_cool_off_after_an_aborted_sweep,
           test_fingerprint_moves_with_what_the_learner_controls,
           test_read_once_ever,
           test_nothing_readable_is_never_cached,
           test_cache_never_raises,
           test_rejected_anchors_are_not_rebought_nightly,
           test_a_billing_cap_is_our_outage,
           test_machinery_never_reaches_a_card,
           test_outage_attempts_do_not_spend_the_ceiling]:
    fn()

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
    sys.exit(1)
print("spend guards: all checks passed")
