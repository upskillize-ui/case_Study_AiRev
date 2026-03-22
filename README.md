# Upskillize AI Case Study Review Agent
### Python + FastAPI | Hugging Face + Claude AI

AI-powered case study evaluation for the PGCDF course.

## Your Stack
| Component | Technology |
|-----------|-----------|
| Agent Server | **Python + FastAPI** |
| Database | MySQL on Avian Cloud (DATABASE_URL) |
| AI (Free) | Hugging Face (Mistral-7B) |
| AI (Paid) | Anthropic Claude |
| Deployment | HuggingFace Spaces / Render |
| Frontend | https://lms.upskillize.com (Netlify) |
| Backend | https://upskillize-lms-backend.onrender.com (Render) |

---

## 🚀 Setup (Step by Step)

### Step 1: Create .env
```bash
cp .env.example .env
```

Fill in:
- `HF_ACCESS_TOKEN` — from huggingface.co/settings/tokens
- `DATABASE_URL` — from Avian Cloud dashboard
- `AGENT_API_KEY` — generate: `python -c "import secrets; print(secrets.token_hex(32))"`

### Step 2: Install
```bash
pip install -r requirements.txt
```

### Step 3: Create database tables
```bash
python sql/run_migrations.py
```

### Step 4: Start the agent
```bash
python main.py
```

Visit: http://localhost:7860/health

### Step 5: Test
```bash
python tests/test_review.py
```

---

## Deploy to HuggingFace Spaces

1. Create a new Space at huggingface.co/new-space
2. Select **Docker** as the SDK
3. Push this code to the Space repo
4. Add secrets in Space Settings: `HF_ACCESS_TOKEN`, `DATABASE_URL`, `AGENT_API_KEY`
5. The Space will auto-build and deploy

## Deploy to Render

1. Push to GitHub
2. Render → New Web Service → Connect repo
3. Build: `pip install -r requirements.txt`
4. Start: `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. Add env variables

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Health check (no auth) |
| POST | `/api/review/submit` | Submit answer & get AI review |
| POST | `/api/review/test` | Test review (no DB save) |
| GET | `/api/review/student-progress/{id}` | Student's current + best scores |
| GET | `/api/review/mentor-dashboard/{id}` | Mentor class overview |
| POST | `/api/review/mentor-approve/{id}` | Mentor approve/override |
| GET | `/api/review/case-studies/{id}` | List case studies |

All endpoints (except /health) require `x-api-key` header.

---

## Project Structure
```
upskillize-ai-agent-python/
├── .env.example
├── .gitignore
├── Dockerfile              # For HuggingFace Spaces
├── requirements.txt
├── main.py                 # FastAPI server entry point
├── app/
│   ├── database.py         # MySQL connection (DATABASE_URL)
│   ├── prompts.py          # AI prompt template
│   ├── models/
│   │   └── schemas.py      # Pydantic request/response models
│   ├── routes/
│   │   └── review.py       # All API endpoints
│   ├── services/
│   │   ├── ai_service.py   # THE BRAIN (HuggingFace + Claude)
│   │   ├── scoring_service.py
│   │   ├── feedback_service.py
│   │   └── db_service.py   # All MySQL operations
│   └── utils/
│       └── text_processor.py
├── sql/
│   ├── migrations.sql      # Database tables
│   └── run_migrations.py   # Table creation script
└── tests/
    └── test_review.py      # Test file
```

## Switch Free → Paid AI
Change one line in `.env`:
```
AI_PROVIDER=huggingface  →  AI_PROVIDER=anthropic
```
