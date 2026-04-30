# app/utils/text_processor.py
# FIXED:
#   1. calculate_text_overlap now uses 4-word shingles (phrase matching),
#      not single-word matching. Single-word matching trips on every
#      thoughtful answer that mentions the case-study terms (Paytm, RBI,
#      compliance, etc.) and was the root cause of "every score = 0".
#   2. Added is_likely_copy() for a clearer plagiarism boolean.
#   3. find_mentioned_concepts is more lenient — handles plural/lowercase.

import re


def count_words(text: str) -> int:
    if not text:
        return 0
    return len(text.strip().split())


def clean_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"[\r\n]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# ── Stop words (kept module-level so we don't rebuild every call) ─────────
_STOP_WORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "can", "shall", "to", "of", "in", "for",
    "on", "with", "at", "by", "from", "as", "and", "but", "or", "not",
    "this", "that", "these", "those", "it", "they", "them", "their",
    "we", "our", "you", "your", "he", "she", "his", "her", "so", "if",
    "than", "then", "into", "about", "over", "such", "also",
})


def _tokens(text: str) -> list[str]:
    """Lowercase content tokens, no stopwords, length > 2."""
    if not text:
        return []
    words = re.findall(r"[a-zA-Z][a-zA-Z\-']+", text.lower())
    return [w for w in words if len(w) > 2 and w not in _STOP_WORDS]


def _shingles(tokens: list[str], n: int = 4) -> set[tuple]:
    """Set of consecutive n-token tuples (shingles). Detects phrase reuse."""
    if len(tokens) < n:
        # For very short texts, fall back to bigrams so overlap is computable
        n = max(2, len(tokens))
    return {tuple(tokens[i : i + n]) for i in range(len(tokens) - n + 1)}


def calculate_text_overlap(student_answer: str, case_study_text: str) -> int:
    """
    Returns 0-100, the % of the student's 4-word phrases that appear
    verbatim in the case study text.

    A real answer that *mentions* "Paytm" and "RBI" and "compliance" will
    score 0% here, because those individual words don't form 4-word phrases
    that match the description verbatim. A copy-paste of a sentence from
    the description will score 80-100%. This is the right behaviour.
    """
    student_tok = _tokens(student_answer)
    case_tok = _tokens(case_study_text)
    if len(student_tok) < 8 or len(case_tok) < 8:
        return 0

    s_shingles = _shingles(student_tok, n=4)
    c_shingles = _shingles(case_tok, n=4)
    if not s_shingles:
        return 0

    overlap = s_shingles & c_shingles
    return round((len(overlap) / len(s_shingles)) * 100)


def is_likely_copy(student_answer: str, case_study_text: str) -> bool:
    """True if the student answer is mostly verbatim from the description."""
    return calculate_text_overlap(student_answer, case_study_text) >= 35


def find_mentioned_concepts(text: str, key_concepts: list[str]) -> dict:
    """Check which key concepts the student mentioned (lenient match)."""
    if not text or not key_concepts:
        return {"mentioned": [], "missing": []}

    lower_text = re.sub(r"\s+", " ", text.lower())
    mentioned, missing = [], []

    for concept in key_concepts:
        if not concept or not isinstance(concept, str):
            continue
        c = concept.strip().lower()
        # Skip "concepts" that are clearly paragraphs (caller should pass
        # short concept names; we guard anyway).
        if len(c.split()) > 6:
            continue
        variations = {
            c,
            c.replace("-", " "),
            c.replace(" ", ""),
            c.rstrip("s"),       # plural form check
            c + "s",
        }
        if any(v and v in lower_text for v in variations):
            mentioned.append(concept)
        else:
            missing.append(concept)

    return {"mentioned": mentioned, "missing": missing}