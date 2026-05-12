# app/services/ai_service.py
# THE BRAIN — calls Hugging Face (free) or Claude (paid)
#
# CHANGES from previous version:
#   - max_tokens dropped 2000 → 1500 (Claude path) — output is now ≤80-word
#     detailedFeedback so we don't need the headroom
#   - System prompt slimmed (was redundant with the user prompt — the long
#     system text was costing every request ~150 input tokens for nothing)
#   - Removed silent fallback to HuggingFace when Anthropic fails — now logs
#     loudly and surfaces the failure in _meta so the UI can show it
#   - Kept everything else (think-tag stripping, HF fallback chain) intact

import os
import re
import time
import httpx
from app.prompts import build_review_prompt, parse_ai_response


# Trimmed system prompt — long mentor instructions live in the user prompt now
SYSTEM_MSG_HF = (
    "You are a warm, precise academic coach for the Upskillize PG Diploma "
    "in FinTech, Banking and AI. Speak directly to the student using 'you'. "
    "Respond with ONLY a valid JSON object — no markdown, no backticks, no preamble."
)
SYSTEM_MSG_CLAUDE = (
    "You are a warm, precise academic coach for the Upskillize PG Diploma "
    "in FinTech, Banking and AI. Speak directly to the student using 'you'. "
    "Respond with ONLY valid JSON."
)


def analyze_answer(
    case_study: dict,
    model_answer,
    student_answer: str,
    grading_rubric: dict,
    key_concepts: list,
) -> dict:
    provider = os.getenv("AI_PROVIDER", "huggingface").lower()
    start_time = time.time()
    print(f"ℹ️  AI provider: {provider}")

    try:
        if provider == "anthropic":
            result, model_used = _analyze_with_claude(
                case_study, model_answer, student_answer, grading_rubric, key_concepts
            )
        else:
            result, model_used = _analyze_with_huggingface(
                case_study, model_answer, student_answer, grading_rubric, key_concepts
            )

        processing_time = int((time.time() - start_time) * 1000)
        print(f"✅ AI review done in {processing_time}ms using {model_used}")

        result["_meta"] = {
            "provider": provider,
            "model": model_used,
            "processingTimeMs": processing_time,
        }
        return result

    except Exception as e:
        # Loud failure logging so silent fallback is impossible to miss
        print(f"❌ Primary AI provider FAILED ({provider}): {e}")

        if provider == "anthropic":
            print("⚠️  FALLING BACK to Hugging Face — Anthropic call failed above.")
            print("   This will be slower and less accurate. Check ANTHROPIC_API_KEY,")
            print("   ANTHROPIC_MODEL, and billing status if this is unexpected.")
            try:
                result, model_used = _analyze_with_huggingface(
                    case_study, model_answer, student_answer, grading_rubric, key_concepts
                )
                result["_meta"] = {
                    "provider": "huggingface_fallback",
                    "model": model_used,
                    "processingTimeMs": int((time.time() - start_time) * 1000),
                    "fallback_reason": str(e)[:200],
                }
                return result
            except Exception as fallback_error:
                print(f"❌ Fallback ALSO failed: {fallback_error}")

        raise


def _strip_think_tags(text: str) -> str:
    """Strip DeepSeek-R1-style reasoning blocks (closed and truncated)."""
    if not text:
        return ""
    text = re.sub(r"<think>[\s\S]*?</think>", "", text)
    text = re.sub(r"<think>[\s\S]*$", "", text)
    return text.strip()


def _analyze_with_huggingface(
    case_study, model_answer, student_answer, grading_rubric, key_concepts
):
    token = os.getenv("HF_ACCESS_TOKEN")
    if not token:
        raise Exception("HF_ACCESS_TOKEN env var is not set")

    prompt = build_review_prompt(
        case_study, model_answer, student_answer, grading_rubric, key_concepts
    )

    models = [
        ("meta-llama/Llama-3.3-70B-Instruct:novita",   1500),
        ("deepseek-ai/DeepSeek-V3-0324:novita",        1500),
        ("deepseek-ai/DeepSeek-R1:novita",             6000),  # reasoning — needs room
    ]

    url = "https://router.huggingface.co/v1/chat/completions"
    last_error = None

    for model, max_tokens in models:
        try:
            print(f"   Trying {model} (max_tokens={max_tokens})...")
            resp = httpx.post(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": SYSTEM_MSG_HF},
                        {"role": "user",   "content": prompt},
                    ],
                    "max_tokens": max_tokens,
                    "temperature": 0.3,
                },
                timeout=120.0,
            )

            if resp.status_code >= 400:
                raise Exception(
                    f"HTTP {resp.status_code} from router: {resp.text[:300]}"
                )

            data = resp.json()
            text = data["choices"][0]["message"]["content"] or ""
            text = _strip_think_tags(text)

            if not text:
                raise Exception(
                    "Model returned empty content (likely truncated inside <think>)"
                )

            parsed = parse_ai_response(text)
            return parsed, model

        except Exception as e:
            print(f"   {model} unavailable: {e}")
            last_error = e
            continue

    raise Exception(f"All AI models are currently unavailable. Last error: {last_error}")


def _analyze_with_claude(
    case_study, model_answer, student_answer, grading_rubric, key_concepts
):
    import anthropic

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    prompt = build_review_prompt(
        case_study, model_answer, student_answer, grading_rubric, key_concepts
    )

    # Default to Haiku 4.5 for speed. Override via ANTHROPIC_MODEL env var.
    model = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

    response = client.messages.create(
        model=model,
        max_tokens=1500,  # was 2000 — output is now ≤80-word detailedFeedback
        system=SYSTEM_MSG_CLAUDE,
        messages=[{"role": "user", "content": prompt}],
    )

    text = response.content[0].text
    parsed = parse_ai_response(text)
    return parsed, model