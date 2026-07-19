# tests/test_prefilter.py
# Pure-function tests for the reflex layer — no DB, no AI.

from app.services.prefilter_service import normalize, fingerprint, detect_abuse


def test_normalize_collapses_case_and_whitespace():
    assert normalize("  Hello   WORLD \n\t x ") == "hello world x"


def test_fingerprint_stable_across_formatting():
    a = fingerprint("The RBI guidelines   change often.\n")
    b = fingerprint("the rbi guidelines change often.")
    assert a == b
    assert fingerprint("different text") != a


def test_abuse_detected_with_word_boundaries():
    assert detect_abuse("this assignment is bullshit honestly") == "bullshit"
    assert detect_abuse("BHOSDIKE review this") == "bhosdike"
    # leetspeak folding
    assert detect_abuse("what the fu5k... wait no: bull5hit") is not None


def test_no_false_positives_on_substrings():
    # 'class', 'assess', 'Scunthorpe-style' substrings must not trip the gate
    assert detect_abuse("we assess the class hierarchy and analyse assets") is None
    assert detect_abuse("the Sassoon fund's passbook shows a hit") is None


def test_clean_academic_text_passes():
    text = ("The KYC process under RBI guidelines requires verification of "
            "identity documents. In my internship at HDFC I observed both "
            "Aadhaar e-KYC and offline verification.")
    assert detect_abuse(text) is None


if __name__ == "__main__":
    import sys, inspect
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and inspect.isfunction(fn):
            try:
                fn()
                print(f"  PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"  FAIL {name}: {e}")
    sys.exit(1 if failures else 0)
