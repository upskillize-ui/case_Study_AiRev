# app/services/ai_service.py
# THE BRAIN — calls Hugging Face (free) or Claude (paid)

import os
import time
from huggingface_hub import InferenceClient
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


def _analyze_with_huggingface(
    case_study, model_answer, student_answer, grading_rubric, key_concepts
):
    token = os.getenv("HF_ACCESS_TOKEN")
    prompt = build_review_prompt(
        case_study, model_answer, student_answer, grading_rubric, key_concepts
    )

    system_msg = (
        "You are a warm, encouraging academic coach for a Post Graduate Diploma in "
        "FinTech, Banking and AI. You evaluate student answers thoughtfully and give "
        "constructive, supportive feedback. Always speak directly to the student using "
        "'you'. Never use harsh language. Be honest but kind. "
        "You MUST respond with ONLY a valid JSON object — no markdown, no backticks, "
        "no explanation. Just pure JSON."
    )

    # Models tried in order — first success wins
    models = [
        ("novita",   "deepseek-ai/DeepSeek-R1-0528"),
        ("novita",   "meta-llama/Llama-3.3-70B-Instruct"),
        ("cerebras", "meta-llama/Llama-3.3-70B-Instruct"),
    ]

    last_error = None

    for provider_name, model in models:
        try:
            print(f"   Trying {provider_name}/{model}...")
            client = InferenceClient(model=model, token=token)

            response = client.chat_completion(
                model=model,
                messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user",   "content": prompt},
                ],
                max_tokens=2000,
                temperature=0.3,
            )

            text = response.choices[0].message.content

            # DeepSeek wraps reasoning in <think> tags — strip them
            import re
            text = re.sub(r"<think>[\s\S]*?</think>", "", text).strip()

            parsed = parse_ai_response(text)
            return parsed, f"{provider_name}/{model}"

        except Exception as e:
            print(f"   {provider_name}/{model} unavailable: {e}")
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

    # FIX: Correct model name
    model = "claude-sonnet-4-6"

    response = client.messages.create(
        model=model,
        max_tokens=2000,
        system=(
            "You are a warm, encouraging academic coach for a Post Graduate Diploma in "
            "FinTech, Banking and AI. Evaluate student answers with kindness and precision. "
            "Always speak directly to the student using 'you'. Never use harsh language. "
            "Respond with ONLY valid JSON."
        ),
        messages=[{"role": "user", "content": prompt}],
    )

    text = response.content[0].text
    parsed = parse_ai_response(text)
    return parsed, model