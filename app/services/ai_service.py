# app/services/ai_service.py
# THE BRAIN — calls Hugging Face (free) or Claude (paid)
#
# FIXED:
#   - Bug #11: handles unclosed <think> tags from DeepSeek-R1 truncation
#   - Bug #10: Anthropic model bumped to claude-sonnet-4-5 (current default)
#   - Better error logging without leaking secrets

import os
import re
import time
import httpx
from app.prompts import build_review_prompt, parse_ai_response


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
        print(f"⚠️  Primary AI provider failed ({provider}): {e}")

        # Graceful fallback to Hugging Face if Claude fails
        if provider == "anthropic":
            print("   Falling back to Hugging Face...")
            try:
                result, model_used = _analyze_with_huggingface(
                    case_study, model_answer, student_answer, grading_rubric, key_concepts
                )
                result["_meta"] = {
                    "provider": "huggingface (fallback)",
                    "model": model_used,
                    "processingTimeMs": int((time.time() - start_time) * 1000),
                }
                return result
            except Exception as fallback_error:
                print(f"⚠️  Fallback also failed: {fallback_error}")

        raise


def _strip_think_tags(text: str) -> str:
    """
    Strip DeepSeek-R1-style reasoning blocks.
    Handles BOTH closed <think>...</think> AND truncated unclosed <think>...
    (Bug #11 fix.)
    """
    if not text:
        return ""
    # First, closed pairs
    text = re.sub(r"<think>[\s\S]*?</think>", "", text)
    # Then, any unclosed leftover (truncation case)
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

    system_msg = (
        "You are a warm, encouraging academic coach for the Upskillize "
        "Post Graduate Diploma in FinTech, Banking and AI. You evaluate "
        "student answers thoughtfully and give constructive, supportive "
        "feedback. Always speak directly to the student using 'you'. Never "
        "use harsh language. Be honest but kind. "
        "You MUST respond with ONLY a valid JSON object — no markdown, no "
        "backticks, no explanation. Just pure JSON."
    )

    # (model_id_with_provider_suffix, max_tokens)
    models = [
        ("meta-llama/Llama-3.3-70B-Instruct:novita",   2000),
        ("deepseek-ai/DeepSeek-V3-0324:novita",        2000),
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
                        {"role": "system", "content": system_msg},
                        {"role": "user",   "content": prompt},
                    ],
                    "max_tokens": max_tokens,
                    "temperature": 0.3,
                },
                timeout=120.0,
            )

            if resp.status_code >= 400:
                # Truncate body to avoid log spam; never logs request headers (which carry the token)
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

    # FIXED: Sonnet 4.5 is the current default. For cheaper bulk reviews
    # use "claude-haiku-4-5". Override via ANTHROPIC_MODEL env var.
    model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")

    response = client.messages.create(
        model=model,
        max_tokens=2000,
        system=(
            "You are a warm, encouraging academic coach for the Upskillize "
            "Post Graduate Diploma in FinTech, Banking and AI. Evaluate "
            "student answers with kindness and precision. Always speak "
            "directly to the student using 'you'. Never use harsh language. "
            "Respond with ONLY valid JSON."
        ),
        messages=[{"role": "user", "content": prompt}],
    )

    text = response.content[0].text
    parsed = parse_ai_response(text)
    return parsed, model