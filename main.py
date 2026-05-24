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

from app.routes.review import router as review_router
from app.routes.assignment_review import router as assignment_router
from app.routes.industry_session_review import router as industry_session_router   # ← ADD THIS
from app.tenants import resolve_tenant_by_key, all_tenant_ids, configured_tenant_ids, TENANTS, Tenant
from app.database import test_all_tenants, set_current_tenant

app = FastAPI(
    title="Upskillize AI Review Agent",
    description="Multi-tenant AI evaluation for case studies, assignments, capstones and industry sessions",
    version="3.1.0",
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
def require_auth_and_tenant(x_api_key: str = Header(default="")) -> Tenant:
    tenant = resolve_tenant_by_key(x_api_key)
    set_current_tenant(tenant)
    return tenant


# ===== Routes =====
app.include_router(review_router,            dependencies=[Depends(require_auth_and_tenant)])
app.include_router(assignment_router,        dependencies=[Depends(require_auth_and_tenant)])
app.include_router(industry_session_router,  dependencies=[Depends(require_auth_and_tenant)])   # ← ADD THIS


# ===== Public endpoints (no auth) =====

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "agent": "upskillize-multi-tenant-reviewer",
        "version": "3.1.0",
        "aiProvider": os.getenv("AI_PROVIDER", "huggingface"),
        "tenants": all_tenant_ids(),
        "tenantsConfigured": configured_tenant_ids(),
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


# ===== Startup =====
@app.on_event("startup")
async def startup():
    print("")
    print("🚀 Upskillize AiRev Agent v3.1 (Multi-Tenant — per-tenant keys)")
    print(f"   AI Provider     : {os.getenv('AI_PROVIDER', 'huggingface')}")
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


# ===== DEBUG (remove after diagnosis) =====
@app.get("/api/debug/keycheck")
async def debug_keycheck(x_api_key: str = Header(default="")):
    received = x_api_key
    received_len = len(received)
    received_preview = received[:6] + "..." + received[-4:] if received_len > 10 else received
    tenants_info = []
    for tid, tenant in TENANTS.items():
        try:
            stored = tenant.api_key
            stored_len = len(stored)
            stored_preview = stored[:6] + "..." + stored[-4:] if stored_len > 10 else stored
            matches = stored == received
            tenants_info.append({"tenant_id": tid, "env_var_name": tenant.api_key_env, "stored_length": stored_len, "stored_preview": stored_preview, "matches_received": matches})
        except RuntimeError as e:
            tenants_info.append({"tenant_id": tid, "env_var_name": tenant.api_key_env, "error": str(e)})
    return {"received_x_api_key": {"length": received_len, "preview": received_preview, "is_empty": received_len == 0}, "tenants": tenants_info}
# ===== END DEBUG =====


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 7860))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)