# main.py
# Upskillize AI Case Study Review Agent — Python + FastAPI
# UI has been removed — React frontend (CaseStudyReview.jsx) handles all UI

import os
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
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
    allow_origins=[
        "https://lms.upskillize.com",
        "https://upskillize.com",
        "https://upskillize-lms-backend.onrender.com",
        "http://localhost:3000",
        "http://localhost:5173",
        "http://localhost:7860",
    ],
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
        "stack": "Python + FastAPI",
        "ui": "Served by React frontend (CaseStudyReview.jsx)",
    }


# ===== Startup =====
@app.on_event("startup")
async def startup():
    print("")
    print("🚀 Upskillize AI Case Study Review Agent v2.0")
    print(f"   AI Provider : {os.getenv('AI_PROVIDER', 'huggingface')}")
    print(f"   Frontend    : https://lms.upskillize.com (React)")
    print(f"   Backend     : https://upskillize-lms-backend.onrender.com/api")
    print("")
    print("   Testing database connection...")
    db_ok = test_connection()
    print(f"   Database: {'✅ Connected' if db_ok else '❌ Not connected'}")
    print("")
    print("   Available endpoints:")
    print("   POST /api/review/submit              — Submit & get AI feedback")
    print("   POST /api/review/test                — Test review (no DB)")
    print("   GET  /api/review/student-progress/id — Student progress")
    print("   GET  /api/review/mentor-dashboard/id — Mentor dashboard")
    print("   POST /api/review/mentor-approve/id   — Mentor approve")
    print("   GET  /api/review/case-studies/id     — List case studies")
    print("   GET  /health                         — Health check")
    print("")


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 7860))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)