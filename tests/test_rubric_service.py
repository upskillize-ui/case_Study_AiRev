"""Scoring math is pure and must be tested — repo standard."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

from app.services.rubric_service import normalise, source_hash, _as_int, FALLBACK_CRITERIA


def test_weights_always_total_100():
    import random
    random.seed(7)
    for _ in range(2000):
        n = random.randint(1, 8)
        crit = [{"name": f"C{i}", "maxScore": random.randint(1, 900)} for i in range(n)]
        assert sum(c["maxScore"] for c in normalise(crit)) == 100


def test_more_than_six_criteria_are_capped():
    crit = [{"name": f"C{i}", "maxScore": 10} for i in range(60)]
    out = normalise(crit)
    assert len(out) <= 6
    assert sum(c["maxScore"] for c in out) == 100


def test_malformed_input_falls_back():
    for bad in (None, [], [{"name": "", "maxScore": 5}], [{"maxScore": 5}],
                [{"name": "A", "maxScore": 0}], [{"name": "A", "maxScore": "x"}]):
        out = normalise(bad)
        assert sum(c["maxScore"] for c in out) == 100
        assert len(out) == len(FALLBACK_CRITERIA)


def test_float_string_weights_are_tolerated():
    out = normalise([{"name": "A", "maxScore": "33.3"}, {"name": "B", "maxScore": "66.7"}])
    assert sum(c["maxScore"] for c in out) == 100


def test_weight_matches_maxscore():
    out = normalise([{"name": "A", "maxScore": 60}, {"name": "B", "maxScore": 40}])
    assert all(abs(c["weight"] - c["maxScore"] / 100) < 1e-9 for c in out)


def test_hash_is_stable_and_edit_sensitive():
    t = {"title": "T", "description": "D", "questions": [], "maxScore": 10}
    assert source_hash(t) == source_hash(dict(t))
    assert source_hash(t) != source_hash({**t, "description": "D2"})


def test_hash_survives_decimal_marks():
    import decimal
    source_hash({"title": "T", "description": "D", "questions": [],
                 "maxScore": decimal.Decimal("10.00")})


def test_as_int_is_total():
    assert _as_int("33.3") == 33 and _as_int(None) == 0 and _as_int("x") == 0
