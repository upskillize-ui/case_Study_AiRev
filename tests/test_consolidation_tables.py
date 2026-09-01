"""Every table consolidation writes must be a table it creates.

THE BUG THIS GUARDS (1 Sep 2026). The gate-tuning table was called
`agent_config`. The lms tenant DB already had a table of that name, owned by
something else and shaped differently. CREATE TABLE IF NOT EXISTS checks the
NAME and never the columns, so it quietly did nothing, and every write failed:

    Consolidation failed for tenant lms: (1054, "Unknown column 'k' in 'field list'")

Every night. And the read side hid it — get_config_float() swallows the
exception and returns the default, so the tuning silently never applied while
the log claimed the gate had been tuned.

The rename to `airev_agent_config` fixed it. This test stops the name drifting
apart again: a CREATE under one name and a write under another is exactly the
shape of the original fault, and it is invisible at runtime.
"""

import pathlib
import re

SRC = (pathlib.Path(__file__).resolve().parents[1]
       / "app" / "services" / "consolidation_service.py").read_text(encoding="utf-8")

# Comments describe the bug and name the old table on purpose; only real SQL
# counts. And only inside SQL: a bare `FROM` regex over Python also matches
# `from app.database import query`, which is how the first draft of this test
# reported `typing` as a missing table.
SQL = "\n".join(
    frag for frag in re.findall(r'"""(.*?)"""|"([^"\n]*)"|\'([^\'\n]*)\'',
                                SRC, re.S)
    for frag in frag if frag and re.search(r"\b(SELECT|INSERT|REPLACE|CREATE)\b",
                                           frag, re.I))

CREATED = set(re.findall(r"CREATE TABLE IF NOT EXISTS\s+(\w+)", SQL, re.I))
WRITTEN = set(re.findall(r"(?:REPLACE|INSERT)\s+INTO\s+(\w+)", SQL, re.I))
READ = set(re.findall(r"\bFROM\s+(\w+)", SQL, re.I))


def test_every_written_table_is_created():
    assert WRITTEN, "no writes found — has the module moved?"
    assert WRITTEN <= CREATED, (
        f"writes to tables this module never creates: {sorted(WRITTEN - CREATED)}. "
        "CREATE TABLE IF NOT EXISTS matches on NAME only, so a table someone else "
        "owns is silently accepted and every write fails at runtime."
    )


def test_every_read_table_is_created():
    assert READ <= CREATED, (
        f"reads from tables this module never creates: {sorted(READ - CREATED)}"
    )


def test_config_table_stays_namespaced():
    """A generic name is what caused the collision. Keep the prefix."""
    assert "airev_agent_config" in CREATED
    assert "agent_config" not in CREATED, (
        "renamed back to the generic name — this is the exact collision that "
        "killed the nightly consolidation on lms"
    )


def test_no_table_name_is_generic_enough_to_collide():
    """Every table here lives in a TENANT database shared with the LMS, so a
    bare noun is a name another system can plausibly claim. Either prefix it or
    make it specific enough that nothing else would choose it."""
    TOO_GENERIC = {"config", "settings", "stats", "notes", "cache", "meta",
                   "agent_config", "exemplars"}
    clashes = {t for t in CREATED if t.lower() in TOO_GENERIC}
    assert not clashes, f"table names another system could already own: {sorted(clashes)}"
