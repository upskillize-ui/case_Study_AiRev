# app/prompts.py
# The AI prompt — evaluates like an expert professor but speaks like a warm mentor.
#
# This version asks the AI to:
#   1. Score EACH rubric criterion BY NAME (no more brittle keyword matching)
#   2. Detect garbage / irrelevant / nonsense submissions
#   3. Estimate how likely the answer is AI-generated vs human-written
#   4. Surface the same warm, structured feedback as before

import json


def build_review_prompt(
    case_study: dict,
    model_answer,
    student_answer: str,
    grading_rubric: dict,
    key_concepts: list,
) -> str:
    model_answer_str = (
        model_answer if isinstance(model_answer, str) else json.dumps(model_answer)
    )
    questions_str  = json.dumps(case_study.get("questions", []))
    concepts_str   = ", ".join(key_concepts) if isinstance(key_concepts, list) else str(key_concepts)

    # Build the rubric criteria list for the prompt + an example block
    criteria = grading_rubric.get("criteria", []) or []
    criteria_lines = "\n".join(
        f"  - \"{c.get('name','Criterion')}\" (max {c.get('maxScore', 25)} points)"
        for c in criteria
    )
    example_criterion_scores = ", ".join(
        f"\"{c.get('name','Criterion')}\": <number 0-100>" for c in criteria
    )

    return f"""You are an expert academic mentor and evaluator for the Post Graduate Diploma in FinTech, Banking & AI (PGCDF) at Upskillize.

Your role is to evaluate student answers with the precision of a seasoned professor and the warmth of a supportive mentor.

TONE RULES (strictly follow):
- Always speak directly to the student using "you" and "your"
- Be encouraging but honest — do not inflate scores
- Replace harsh phrases with constructive ones (e.g. "this could be more accurate" instead of "wrong")
- Celebrate genuine effort; never manufacture praise for empty submissions

=== CASE STUDY ===
Title: {case_study.get('title', '')}
Description: {case_study.get('description', '')}
Questions: {questions_str}

=== IDEAL ANSWER (reference, not the only correct answer) ===
{model_answer_str}

=== KEY CONCEPTS (the student should ideally mention these) ===
{concepts_str}

=== RUBRIC CRITERIA (you MUST score each one BY NAME, 0-100) ===
{criteria_lines}

=== STUDENT'S SUBMITTED ANSWER ===
{student_answer}

=== YOUR EVALUATION TASK ===
Respond with ONLY a valid JSON object. No markdown, no backticks, no preamble.

The JSON shape MUST be:

{{
  "isGarbage": <true if the answer is nonsense/spam/empty/joke/single-word/unrelated, false otherwise>,
  "garbageReason": "<if isGarbage, briefly say why; otherwise empty string>",

  "criterionScores": {{ {example_criterion_scores} }},

  "relevanceScore":   <number 0-100>,
  "depthScore":       <number 0-100>,
  "applicationScore": <number 0-100>,
  "accuracyScore":    <number 0-100>,
  "structureScore":   <number 0-100>,

  "conceptsCovered":  ["concepts the student DID mention"],
  "conceptsMissing":  ["concepts the student did NOT mention"],

  "strengths": [
    "Specific genuine strength 1",
    "Specific genuine strength 2",
    "Specific genuine strength 3"
  ],
  "improvements": [
    "Constructive suggestion 1",
    "Constructive suggestion 2",
    "Constructive suggestion 3"
  ],
  "detailedFeedback": "2-3 warm paragraphs of feedback. Acknowledge what was done well, explain what to improve, close with encouragement.",

  "plagiarismRisk": "low" | "medium" | "high",
  "plagiarismNote": "<only if medium/high; otherwise empty string>",

  "aiLikelihoodPercent": <integer 0-100, your best estimate of how likely the text was written by an AI/LLM (e.g. ChatGPT). 0 = clearly human, 100 = clearly AI>,
  "aiDetectionReason":   "<one short sentence: which textual signals informed your estimate (vocabulary uniformity, sentence length variance, hedging patterns, hallucinated specifics, perfect formatting, etc.)>",

  "suggestedTopics":   ["topic 1", "topic 2", "topic 3"],
  "mentorAlert":       <true if score will likely be below 40 OR isGarbage is true>,
  "mentorAlertReason": "<if mentorAlert, brief professional reason; otherwise empty>"
}}

EVALUATION PRINCIPLES:
1. If the answer is empty, single-word, repeated characters, or wholly unrelated to the case study — set isGarbage=true and give every criterionScore a 0.
2. Score each rubric criterion strictly on its NAME and meaning, judging the student's text against that specific dimension.
3. For aiLikelihoodPercent: be honest. Very polished, hedge-heavy, evenly-paced text with no personal voice often indicates AI. Spelling errors, varied rhythm, opinionated phrasing, and locally-rooted examples often indicate human writing. State your best estimate — do not refuse.
4. Award credit for genuine real-world examples and original thinking even when different from the ideal answer.
5. Never reproduce the student's exact wording in feedback.
"""


def parse_ai_response(text: str) -> dict:
    """Parse AI response into a dict with safe defaults."""
    import re

    defaults = {
        "isGarbage": False,
        "garbageReason": "",
        "criterionScores": {},
        "relevanceScore": 50,
        "depthScore": 50,
        "applicationScore": 50,
        "accuracyScore": 50,
        "structureScore": 50,
        "conceptsCovered": [],
        "conceptsMissing": [],
        "strengths": [
            "Thank you for taking the time to submit your answer.",
            "Your submission has been received and saved.",
        ],
        "improvements": [
            "Our AI review encountered a temporary issue. A mentor will personally review your answer soon.",
            "In the meantime, try reviewing the course modules related to this case study.",
        ],
        "detailedFeedback": (
            "Thank you for your submission! Your answer has been saved. "
            "Our automated review encountered a temporary issue, but a mentor will personally review your work. 🌟"
        ),
        "plagiarismRisk": "low",
        "plagiarismNote": "",
        "aiLikelihoodPercent": 50,
        "aiDetectionReason": "Unable to assess — defaulting to uncertain.",
        "suggestedTopics": [],
        "mentorAlert": True,
        "mentorAlertReason": "AI parsing issue — mentor review requested.",
    }

    try:
        cleaned = text.strip()
        cleaned = re.sub(r"```json?\s*\n?", "", cleaned)
        cleaned = re.sub(r"```\s*$", "", cleaned)
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if match:
            cleaned = match.group(0)
        parsed = json.loads(cleaned.strip())

        # Coerce aiLikelihoodPercent into a sane integer 0-100
        try:
            ai_pct = int(round(float(parsed.get("aiLikelihoodPercent", 50))))
            parsed["aiLikelihoodPercent"] = max(0, min(100, ai_pct))
        except Exception:
            parsed["aiLikelihoodPercent"] = 50

        # Coerce criterionScores values to ints
        cs = parsed.get("criterionScores", {})
        if isinstance(cs, dict):
            cleaned_cs = {}
            for k, v in cs.items():
                try:
                    cleaned_cs[str(k)] = max(0, min(100, int(round(float(v)))))
                except Exception:
                    cleaned_cs[str(k)] = 50
            parsed["criterionScores"] = cleaned_cs
        else:
            parsed["criterionScores"] = {}

        return {**defaults, **parsed}

    except Exception as e:
        print(f"⚠️  AI response parsing issue: {e}")
        print(f"   Raw text preview: {text[:200] if text else '(empty)'}")
        return defaults