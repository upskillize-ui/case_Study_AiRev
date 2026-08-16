"""Un-nesting rewrites a learner's stored submission, so the logic is proven
before it touches the database.

The nesting was mine: the regrade route read an already-assembled row AND
re-extracted its attachment, wrapping the whole thing again. Student 1126 went
6.8/10 to 1.2/10 on identical input.
"""
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

import repair_nested_notes as rn
from app.utils.submission_intake import MANIFEST_HEADER, from_stored_submission

CLEAN = (
    MANIFEST_HEADER + "\n"
    "The learner submitted 2 item(s):\n"
    "  1. IMAGE — future.png — read\n"
    "  2. TYPED TEXT — answer box — read\n\n"
    "=== ITEM 1: IMAGE (future.png) ===\n"
    "TEXT:\nNEXT 5 YEARS\n\n"
    "=== ITEM 2: TYPED TEXT (answer box) ===\n"
    "My plan is to become a bank manager."
)

NESTED = (
    MANIFEST_HEADER + "\n"
    "The learner submitted 2 item(s):\n"
    "  1. IMAGE — future.png — read\n"
    "  2. TYPED TEXT — answer box — read\n\n"
    "=== ITEM 1: IMAGE (future.png) ===\n"
    "TEXT:\nNEXT 5 YEARS\n\n"
    "=== ITEM 2: TYPED TEXT (answer box) ===\n" + CLEAN
)

TRIPLE = (
    MANIFEST_HEADER + "\nouter\n\n=== ITEM 1: TYPED TEXT (answer box) ===\n" + NESTED
)


def test_a_clean_row_is_left_exactly_as_it_is():
    assert rn.repair(CLEAN) == CLEAN
    assert rn.is_repairable(CLEAN) is False


def test_raw_learner_text_is_untouched():
    raw = "I want to be a data analyst."
    assert rn.repair(raw) == raw
    assert rn.is_repairable(raw) is False


def test_empty_input_is_survivable():
    assert rn.repair("") == ""
    assert rn.repair(None) == ""


def test_nesting_is_detected():
    assert rn.nesting_depth(CLEAN) == 1
    assert rn.nesting_depth(NESTED) == 2
    assert rn.nesting_depth(TRIPLE) == 3


def test_a_nested_row_is_restored_to_the_clean_assembly():
    assert rn.repair(NESTED) == CLEAN
    assert rn.is_repairable(NESTED) is True


def test_three_layers_collapse_to_the_innermost():
    """Each layer wrapped the previous one WHOLE, so the tail is the original."""
    assert rn.repair(TRIPLE) == CLEAN


def test_the_learners_own_words_survive_the_repair():
    """The point of the exercise. Losing them would be worse than the nesting."""
    assert "My plan is to become a bank manager." in rn.repair(NESTED)


def test_the_duplicate_ocr_is_removed():
    assert rn.repair(NESTED).count("NEXT 5 YEARS") == 1
    assert NESTED.count("NEXT 5 YEARS") == 2


def test_the_repaired_row_carries_exactly_one_manifest():
    assert rn.repair(NESTED).count(MANIFEST_HEADER) == 1
    assert rn.repair(TRIPLE).count(MANIFEST_HEADER) == 1


def test_the_repaired_row_is_readable_by_the_agent():
    """The whole point: the fixed row must parse as assembled intake output."""
    out = from_stored_submission(rn.repair(NESTED))
    assert out is not None
    manifest, content = out
    assert "My plan is to become a bank manager." in content
    assert MANIFEST_HEADER not in content


def test_repair_is_idempotent():
    """Running the tool twice must not eat a second layer of real content."""
    once = rn.repair(NESTED)
    assert rn.repair(once) == once


def test_a_stack_of_bare_headers_is_refused():
    """'Repairing' to a manifest with no items is worse than leaving it for a
    human — there would be nothing left to grade."""
    junk = MANIFEST_HEADER + "\nnothing\n" + MANIFEST_HEADER + "\nnothing either"
    assert rn.is_repairable(junk) is False


def test_the_repair_never_grows_the_text():
    for text in (CLEAN, NESTED, TRIPLE, "plain", ""):
        assert len(rn.repair(text)) <= len(text or "")


@pytest.mark.parametrize("stamp", ["2026-08-14", "x;DROP TABLE y", "../../etc"])
def test_the_backup_name_cannot_carry_sql(stamp):
    assert all(c.isalnum() or c == "_" for c in rn.backup_table_name(stamp))
