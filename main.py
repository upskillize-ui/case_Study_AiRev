# main.py
# Upskillize AI Case Study Review Agent — Python + FastAPI
# Standalone agent with its own frontend UI served from /static

import os
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from app.routes.review import router as review_router
from app.database import test_connection

app = FastAPI(
    title="Upskillize AI Case Study Review Agent",
    description="AI-powered case study evaluation for the PGCDF course",
    version="2.0.0",
)

# ===== CORS =====
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # standalone agent — accept from anywhere
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ===== Routes =====
app.include_router(review_router)

# ===== Health Check =====
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "agent": "upskillize-case-study-reviewer",
        "version": "2.0.0",
        "aiProvider": os.getenv("AI_PROVIDER", "huggingface"),
        "ui": "Standalone — visit / for the web UI",
    }

# ===== Serve frontend UI =====
@app.get("/")
async def serve_ui():
    return FileResponse("static/index.html")

app.mount("/static", StaticFiles(directory="static"), name="static")


# ===== Startup =====
@app.on_event("startup")
async def startup():
    print("")
    print("🚀 Upskillize AI Case Study Review Agent v2.0 (Standalone)")
    print(f"   AI Provider : {os.getenv('AI_PROVIDER', 'huggingface')}")
    print(f"   Web UI      : Visit / for the standalone frontend")
    print("")
    print("   Testing database connection...")
    db_ok = test_connection()
    print(f"   Database: {'✅ Connected' if db_ok else '❌ Not connected'}")
    print("")
    print("   Endpoints:")
    print("   GET  /                               — Standalone Web UI")
    print("   POST /api/review/submit              — Submit & get AI feedback")
    print("   GET  /api/review/case-studies         — List published case studies")
    print("   GET  /api/review/student-progress/id  — Student history")
    print("   GET  /health                          — Health check")
    print("")


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 7860))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)