# main.py
# Upskillize AI Case Study Review Agent — FastAPI entrypoint
#
# FIXED:
#   - Bug #3: CORS origins are now explicit (not "*"), credentials disabled.
#   - Bug #4: x-api-key header is now validated on /api/review/* routes.
#   - Methods restricted to what's actually used.

import os
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Header, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from app.routes.review import router as review_router
from app.database import test_connection

app = FastAPI(
    title="Upskillize AI Case Study Review Agent",
    description="AI-powered case study evaluation for the Upskillize LMS",
    version="2.1.0",
)

# ===== CORS =====
# FIXED: explicit origins; "*" is incompatible with credentials and risky.
ALLOWED_ORIGINS = [
    o.strip() for o in os.getenv(
        "ALLOWED_ORIGINS",
        "https://lms.upskillize.com,https://eaprep.upskillize.com,"
        "https://upskillize.com,http://localhost:5173,http://localhost:3000"
    ).split(",")
    if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,                    # using header auth, not cookies
    allow_methods=["GET", "POST", "OPTIONS"],   # explicit, no PUT/DELETE/PATCH
    allow_headers=["Content-Type", "x-api-key"],
)


# ===== Auth dependency =====
# FIXED: every /api/review/* call must present x-api-key matching the
# AGENT_API_KEY env var. Set this on the HF Space (secret) and mirror it
# as VITE_AGENT_API_KEY on Netlify.
def require_api_key(x_api_key: str = Header(default="")):
    expected = os.getenv("AGENT_API_KEY", "")
    # If you haven't set AGENT_API_KEY yet (first deploy), allow through
    # but log loudly — this is a safety net, not a permanent state.
    if not expected:
        print("⚠️  AGENT_API_KEY is not set — agent is publicly callable!")
        return
    if x_api_key != expected:
        raise HTTPException(status_code=401, detail="invalid or missing x-api-key")


# ===== Routes =====
app.include_router(review_router, dependencies=[Depends(require_api_key)])


# ===== Health Check (no auth — used for uptime checks) =====
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "agent": "upskillize-case-study-reviewer",
        "version": "2.1.0",
        "aiProvider": os.getenv("AI_PROVIDER", "huggingface"),
        "authConfigured": bool(os.getenv("AGENT_API_KEY")),
        "ui": "Visit / for the standalone web UI",
    }


# ===== Serve frontend UI (no auth — public landing) =====
@app.get("/")
async def serve_ui():
    return FileResponse("static/index.html")

app.mount("/static", StaticFiles(directory="static"), name="static")


# ===== Startup =====
@app.on_event("startup")
async def startup():
    print("")
    print("🚀 Upskillize AiRev Agent v2.1 (Standalone)")
    print(f"   AI Provider     : {os.getenv('AI_PROVIDER', 'huggingface')}")
    print(f"   API Key Auth    : {'ENABLED' if os.getenv('AGENT_API_KEY') else 'DISABLED — set AGENT_API_KEY!'}")
    print(f"   Allowed Origins : {ALLOWED_ORIGINS}")
    print(f"   Web UI          : Visit / for the standalone frontend")
    print("")
    print("   Testing database connection...")
    db_ok = test_connection()
    print(f"   Database: {'✅ Connected' if db_ok else '❌ Not connected'}")
    print("")


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 7860))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)