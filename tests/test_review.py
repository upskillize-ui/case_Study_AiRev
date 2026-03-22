# tests/test_review.py
# Test the AI review — run: python tests/test_review.py
# Make sure the server is running first: python main.py

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from dotenv import load_dotenv
load_dotenv()

AGENT_URL = os.getenv("AGENT_URL", "http://localhost:7860")
API_KEY = os.getenv("AGENT_API_KEY", "paste-your-generated-key-here")

HEADERS = {"x-api-key": API_KEY, "Content-Type": "application/json"}


def test_health():
    print("--- Test 1: Health Check ---")
    try:
        r = httpx.get(f"{AGENT_URL}/health", timeout=10)
        print(f"✅ Health: {r.json()}")
        return True
    except Exception as e:
        print(f"❌ Health failed. Is the server running? {e}")
        return False


def test_good_answer():
    print("\n--- Test 2: Review a GOOD answer ---")
    data = {
        "studentAnswer": """The RBI Digital Lending Guidelines of 2022 have significantly transformed the FinTech lending landscape in India. For startups like XYZ FinTech, these guidelines impose several key requirements.

First, the FLDG (First Loss Default Guarantee) model has been capped at 5%, which directly affects how FinTech companies partner with NBFCs and banks. Previously, FinTechs could offer higher guarantees, making it easier to originate loans.

Second, eKYC requirements have become more stringent. Video KYC and Aadhaar-based verification are now mandatory, increasing compliance costs but also building customer trust. Data privacy requirements under the guidelines align with the upcoming Digital Personal Data Protection Act.

The impact on customer acquisition cost is significant. With mandatory disclosure of interest rates and cooling-off periods, customers are better informed.

However, there are clear opportunities. Regulated markets create barriers for fly-by-night operators. XYZ FinTech should adopt RegTech solutions for automated compliance monitoring, strengthen banking partnerships, and invest in robust data governance frameworks.

In conclusion, while the guidelines create short-term challenges, they establish a more sustainable digital lending ecosystem.""",
        "caseStudy": {
            "title": "Digital Lending Revolution in India",
            "description": "XYZ FinTech is a digital lending startup that provides instant personal loans through a mobile app.",
            "questions": ["Analyse the impact of RBI digital lending guidelines on FinTech startups."],
        },
        "modelAnswer": "Should cover RBI Digital Lending Guidelines 2022, FLDG model impact, eKYC, data privacy, customer acquisition cost, RegTech adoption.",
        "gradingRubric": {
            "criteria": [
                {"name": "Understanding of Concepts", "maxScore": 30, "weight": 0.3},
                {"name": "Application and Analysis", "maxScore": 30, "weight": 0.3},
                {"name": "Real-World Examples", "maxScore": 20, "weight": 0.2},
                {"name": "Structure and Clarity", "maxScore": 20, "weight": 0.2},
            ]
        },
        "keyConcepts": ["RBI Digital Lending Guidelines", "FLDG", "eKYC", "data privacy", "RegTech", "NBFC"],
        "wordLimitMin": 300,
        "wordLimitMax": 500,
    }

    try:
        r = httpx.post(f"{AGENT_URL}/api/review/test", json=data, headers=HEADERS, timeout=120)
        result = r.json()
        if result.get("success"):
            res = result["result"]
            print(f"✅ Score: {res['score']}/100")
            print(f"   Grade: {res['grade']}")
            print(f"   AI Model: {res.get('aiMeta', {}).get('model', 'N/A')}")
            print(f"   Time: {res.get('aiMeta', {}).get('processingTimeMs', 'N/A')}ms")
            print(f"   Strengths: {res['feedback']['strengths'][:2]}")
            print(f"   Improvements: {res['feedback']['improvements'][:2]}")
        else:
            print(f"❌ Review failed: {result}")
    except Exception as e:
        print(f"❌ Error: {e}")


def test_weak_answer():
    print("\n--- Test 3: Review a WEAK answer ---")
    data = {
        "studentAnswer": "RBI made some rules for digital lending. FinTech companies need to follow them. This is important.",
        "caseStudy": {
            "title": "Digital Lending Revolution in India",
            "description": "XYZ FinTech is a digital lending startup...",
            "questions": ["Analyse the impact of RBI digital lending guidelines."],
        },
        "modelAnswer": "Should cover FLDG, eKYC, RegTech, data privacy.",
        "gradingRubric": {
            "criteria": [
                {"name": "Understanding of Concepts", "maxScore": 30, "weight": 0.3},
                {"name": "Application and Analysis", "maxScore": 30, "weight": 0.3},
                {"name": "Real-World Examples", "maxScore": 20, "weight": 0.2},
                {"name": "Structure and Clarity", "maxScore": 20, "weight": 0.2},
            ]
        },
        "keyConcepts": ["FLDG", "eKYC", "RegTech", "NBFC", "data privacy"],
        "wordLimitMin": 300,
        "wordLimitMax": 500,
    }

    try:
        r = httpx.post(f"{AGENT_URL}/api/review/test", json=data, headers=HEADERS, timeout=120)
        result = r.json()
        if result.get("success"):
            res = result["result"]
            print(f"✅ Score: {res['score']}/100")
            print(f"   Grade: {res['grade']}")
            print(f"   Needs Help: {res['feedback'].get('needsAttention', 'N/A') if isinstance(res['feedback'], dict) else 'N/A'}")
        else:
            print(f"❌ {result}")
    except Exception as e:
        print(f"❌ Error: {e}")


if __name__ == "__main__":
    print("🧪 Testing Upskillize AI Agent (Python + FastAPI)\n")
    if test_health():
        test_good_answer()
        test_weak_answer()
    print("\n🎉 Tests complete!")
