# app/utils/text_processor.py

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


def calculate_text_overlap(student_answer: str, case_study_text: str) -> int:
    """Check if student copied from the case study."""
    if not student_answer or not case_study_text:
        return 0

    stop_words = {
        "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "do", "does", "did", "will", "would", "could",
        "should", "may", "might", "can", "shall", "to", "of", "in", "for",
        "on", "with", "at", "by", "from", "as", "and", "but", "or", "not",
        "this", "that", "these", "those", "it", "they", "them", "their",
        "we", "our", "you", "your", "he", "she", "his", "her", "so",
    }

    student_words = [w.lower() for w in student_answer.split() if len(w) > 3 and w.lower() not in stop_words]
    case_words = set(w.lower() for w in case_study_text.split())

    if not student_words:
        return 0

    overlap = [w for w in student_words if w in case_words]
    return round((len(overlap) / len(student_words)) * 100)


def find_mentioned_concepts(text: str, key_concepts: list[str]) -> dict:
    """Check which key concepts the student mentioned."""
    if not text or not key_concepts:
        return {"mentioned": [], "missing": []}

    lower_text = text.lower()
    mentioned = []
    missing = []

    for concept in key_concepts:
        variations = [
            concept.lower(),
            concept.lower().replace("-", "").replace(" ", ""),
            concept.lower().replace("-", " "),
        ]
        found = any(v in lower_text for v in variations)
        if found:
            mentioned.append(concept)
        else:
            missing.append(concept)

    return {"mentioned": mentioned, "missing": missing}
