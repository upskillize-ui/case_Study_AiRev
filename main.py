# main.py
# Upskillize AI Review Agent — multi-tenant entrypoint
#
# v3.1 — added industry_session_review router (4th review type)

import os
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from fastapi.responses import JSONResponse
from app.routes.review import router as review_router
from app.routes.assignment_review import router as assignment_router
from app.routes.industry_session_review import router as industry_session_router   # ← ADD THIS
from app.routes.exceptions import router as exceptions_router
from app.routes.review_jobs import router as review_jobs_router, resume_after_restart
from app.services.capacity import CapacityFull, BUSY_MESSAGE, RETRY_AFTER_SECONDS, snapshot as capacity_snapshot
from app.tenants import resolve_tenant_by_key, all_tenant_ids, configured_tenant_ids, TENANTS, Tenant
from app.database import test_all_tenants, set_current_tenant

# docs_url / redoc_url / openapi_url are DISABLED by default.
#
# FastAPI publishes them with no auth, and this Space is on the public
# internet. The scan in the 14 Aug log — /.env, /.env.local, /.streamlit/
# secrets.toml, /file%3D../.env, /api/predict — also fetched /openapi.json and
# got 200: the complete route map, every path parameter and every request
# schema, handed to whoever asked. The routes themselves are auth-gated, so
# this is not a breach; it is a free map of the building for anyone planning
# one, and there is no reason to publish it.
#
# Set ENABLE_API_DOCS=1 temporarily when you need Swagger while developing.
_DOCS_ON = os.getenv("ENABLE_API_DOCS", "").strip().lower() in {"1", "true", "yes", "on"}

app = FastAPI(
    title="Upskillize AI Review Agent",
    description="Multi-tenant AI evaluation for case studies, assignments, capstones and industry sessions",
    version="3.1.0",
    docs_url="/docs" if _DOCS_ON else None,
    redoc_url="/redoc" if _DOCS_ON else None,
    openapi_url="/openapi.json" if _DOCS_ON else None,
)

# ===== CORS =====
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
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "x-api-key"],
)


# ===== Auth + tenant resolution dependency =====
# ASYNC, and it MUST stay async. FastAPI runs a SYNC dependency in a worker
# thread via anyio, which gives it a COPY of the contextvar context — so
# set_current_tenant() wrote the tenant into a context that was discarded the
# moment the dependency returned, and every query()/execute() in the request
# then fell through _resolve_url()'s "lms" default. Measured: a sync dep leaves
# the handler's contextvar None; an async dep propagates it. Effect while it
# was sync: an eaprep key read AND WROTE the lms production database.
async def require_auth_and_tenant(x_api_key: str = Header(default="")) -> Tenant:
    tenant = resolve_tenant_by_key(x_api_key)
    set_current_tenant(tenant)
    return tenant


# ===== Routes =====
app.include_router(review_router,            dependencies=[Depends(require_auth_and_tenant)])
app.include_router(assignment_router,        dependencies=[Depends(require_auth_and_tenant)])
app.include_router(industry_session_router,  dependencies=[Depends(require_auth_and_tenant)])   # ← ADD THIS
app.include_router(exceptions_router,        dependencies=[Depends(require_auth_and_tenant)])
app.include_router(review_jobs_router,       dependencies=[Depends(require_auth_and_tenant)])


# ===== Public endpoints (no auth) =====

@app.exception_handler(CapacityFull)
async def _capacity_full_handler(request, exc):
    # HTTP 503 + Retry-After for correctness with any caller; body carries
    # blocked="capacity" so the AiRev panel renders the courteous notice using
    # its existing blocked-response handling.
    return JSONResponse(
        status_code=503,
        headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
        content={"success": False, "blocked": "capacity", "busy": True,
                 "retryAfterSeconds": RETRY_AFTER_SECONDS, "message": BUSY_MESSAGE},
    )


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "agent": "upskillize-multi-tenant-reviewer",
        "version": "3.1.0",
        "aiProvider": os.getenv("AI_PROVIDER", "huggingface"),
        "model": _model_tiers()["default"](),
        "tenants": all_tenant_ids(),
        "tenantsConfigured": configured_tenant_ids(),
        "load": capacity_snapshot(),
    }


@app.get("/api/tenants")
async def list_tenants():
    return {
        "tenants": [
            {"id": t.id, "name": t.name, "label": t.label, "configured": t.has_api_key()}
            for t in TENANTS.values()
        ]
    }


@app.get("/")
async def serve_ui():
    return FileResponse("static/index.html")

app.mount("/static", StaticFiles(directory="static"), name="static")


# ===== Nightly consolidation (the agent's sleep) =====
# HF Spaces have no cron; APScheduler runs in-process. 21:00 UTC = 02:30 IST.
# Idempotent job — a Space restart mid-cycle is harmless. Manual trigger:
# POST /api/admin/consolidate with x-admin-key = ADMIN_JOB_KEY.

def _start_scheduler():
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger
        from app.services.consolidation_service import run_all_tenants

        from app.services.sweeper_service import sweep_all_tenants

        scheduler = AsyncIOScheduler()
        scheduler.add_job(run_all_tenants, CronTrigger(hour=21, minute=0),
                          id="nightly_consolidation", replace_existing=True)
        # THE SAFETY NET. Runs BEFORE consolidation so the night's marks are
        # in before the agent reflects on them, and every three hours besides:
        # a learner who submits at 10am should not wait until 2:30am for the
        # queue to have dropped their row. Cheap by construction — a healthy
        # cohort finds nothing, and MAX_PER_RUN caps a bad night.
        scheduler.add_job(sweep_all_tenants, CronTrigger(hour="*/3", minute=30),
                          id="unreviewed_sweep", replace_existing=True)
        scheduler.start()
        print("   🌙 Nightly consolidation scheduled (21:00 UTC / 02:30 IST)")
        print("   🧹 Unreviewed sweep scheduled (every 3h) — no submission "
              "is left without a mark")
    except Exception as e:
        print(f"   ⚠️ Scheduler unavailable: {e} — use POST /api/admin/consolidate")


@app.post("/api/admin/consolidate")
async def trigger_consolidation(x_admin_key: str = Header(default="")):
    expected = os.getenv("ADMIN_JOB_KEY", "")
    if not expected or x_admin_key != expected:
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="invalid x-admin-key")
    from app.services.consolidation_service import run_all_tenants
    import asyncio
    result = await asyncio.to_thread(run_all_tenants)
    return {"success": True, "summary": result}


def _model_tiers():
    """Deferred import — keeps main.py importable before services are ready."""
    from app.services.ai_service import MODEL_TIERS
    return MODEL_TIERS


def _print_model_policy() -> None:
    """Print every CLAUDE model this process will call, and shout if any is
    off-policy. A Sonnet default hid in the OCR path for weeks and only
    surfaced on the billing page — the startup log is where that belongs.

    Whisper (TRANSCRIBE_MODEL) is printed for visibility but not policed:
    it is a different provider with its own pricing.
    """
    from app.utils.file_extractor import OCR_MODEL
    tiers = _model_tiers()

    claude = {
        "reviews":     tiers["default"](),
        "escalation":  tiers["strong"](),   # also knowledge builds + consolidation
        "ocr":         OCR_MODEL,
    }
    allow = os.getenv("ALLOWED_MODEL_PREFIX", "claude-haiku")
    print("   Models          : " + " · ".join(f"{k}={v}" for k, v in claude.items())
          + f" · transcribe={os.getenv('TRANSCRIBE_MODEL', 'whisper-1')}")
    off = sorted({v for v in claude.values() if not v.startswith(allow)})
    if off:
        print(f"   ⚠️  OFF-POLICY MODEL IN USE: {', '.join(off)} "
              f"(policy prefix '{allow}') — this WILL cost more than Haiku.")

    # WHERE the calls go matters as much as WHICH model answers them: a gateway
    # sees every submission we send, and a silently-empty chain means every
    # review quietly degrades to HuggingFace. Print the order, so a missing
    # secret is caught in the startup log rather than on a bill or in a grade.
    from app.services.ai_service import providers
    chain = providers()
    if not chain:
        print("   ⚠️  NO CLAUDE PROVIDER CONFIGURED — every review will fall "
              "back to HuggingFace. Set STARTUPAPI_API_KEY + STARTUPAPI_BASE_URL "
              "and/or ANTHROPIC_API_KEY.")
    else:
        order = " → ".join(
            f"{i+1}.{p.name}({p.base_url or 'api.anthropic.com'})"
            for i, p in enumerate(chain))
        print(f"   Claude providers: {order}")
        if len(chain) == 1:
            print(f"   ⚠️  NO FALLBACK: '{chain[0].name}' is the only provider. "
                  f"If it runs out of balance, reviews drop to HuggingFace.")
        hosts = [p.base_url for p in chain if p.base_url]
        if len(hosts) != len(set(hosts)):
            print("   ⚠️  Two providers share a base URL — the fallback points at "
                  "the same host as the primary and will fail with it.")


# ===== Startup =====
@app.on_event("startup")
async def startup():
    # Route handlers are sync `def` (blocking DB/AI/file work), so FastAPI runs
    # them in the anyio threadpool — the event loop stays free for /health and
    # the list endpoints even while long reviews run. Default pool is 40; raise
    # it to match LIVE_CAPACITY so concurrent reviews don't queue behind it.
    try:
        import anyio
        pool = int(os.getenv("THREADPOOL_LIMIT", os.getenv("LIVE_CAPACITY", "100")))
        anyio.to_thread.current_default_thread_limiter().total_tokens = pool
        print(f"   Threadpool limit : {pool} concurrent blocking handlers")
    except Exception as e:
        print(f"   ⚠️ threadpool tune skipped: {e}")

    _start_scheduler()
    print("")
    print("🚀 Upskillize AiRev Agent v3.1 (Multi-Tenant — per-tenant keys)")
    print(f"   AI Provider     : {os.getenv('AI_PROVIDER', 'huggingface')}")
    _print_model_policy()
    print(f"   Allowed Origins : {ALLOWED_ORIGINS}")
    print(f"   Registered      : {all_tenant_ids()}")
    configured = configured_tenant_ids()
    print(f"   Configured      : {configured}")
    print(f"   Review types    : Case Studies · Assignments · Capstones · Industry Sessions")
    print(f"   Web UI          : Visit / for the standalone frontend")
    print("")
    if not configured:
        print("   NO TENANTS CONFIGURED. Set <TENANT>_API_KEY and <TENANT>_DATABASE_URL")
        print("       env vars on the HF Space, then restart.")
        print("")
        return
    print("   Testing tenant database connections...")
    results = test_all_tenants()
    ok = sum(1 for v in results.values() if v)
    print(f"   {ok}/{len(results)} tenant DBs connected")
    print("")
    # Deploys restart the Space mid-cohort; a flagged-on queue picks its job
    # back up from DB state instead of leaving half a class unmarked.
    try:
        resume_after_restart()
    except Exception as e:
        print(f"   ⚠️ review-jobs resume skipped: {e}")


# NOTE: /api/debug/keycheck was removed on 12 Aug 2026.
# It required no auth and returned each tenant key's length and its first
# six / last four characters to any caller on the internet. The startup log
# already reports which tenants are configured; that is the safe equivalent.


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 7860))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)