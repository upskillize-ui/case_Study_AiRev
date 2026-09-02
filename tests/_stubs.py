# tests/_stubs.py
# ---------------------------------------------------------------------------
# Import a module under test from a checkout that may not carry every sibling.
#
# The pure functions these suites exercise touch no model, no DB and no
# network — but the modules holding them import neighbours that do, at import
# time. A missing neighbour is stubbed so the test still runs; in a full
# checkout nothing is stubbed and this is a plain import.
#
# Test-only. Never imported by application code.
# ---------------------------------------------------------------------------

import importlib
import sys
import types

# A live key in the environment would let an import-time client turn a unit
# test into a billed call. Nothing here needs one.
for _k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
    __import__("os").environ.pop(_k, None)


def import_with_stubs(path: str, names=(), tries: int = 12):
    """Import `path`, stubbing absent `app.*` neighbours. Returns the module,
    or the named attributes when `names` is given."""
    for _ in range(tries):
        try:
            mod = importlib.import_module(path)
            return [getattr(mod, n) for n in names] if names else mod
        except ModuleNotFoundError as e:
            missing = e.name
            if not missing or not missing.startswith("app."):
                raise
            stub = types.ModuleType(missing)
            stub.__getattr__ = lambda _n: ""
            sys.modules[missing] = stub
    raise ImportError(f"could not import {path}")
