# main.py
# Upskillize AI Review Agent — multi-tenant entrypoint
#
# v3.0 (Option C — per-tenant API keys):
#   - Auth dep validates x-api-key by looking up which tenant owns it
#   - No X-Tenant-Id header needed (tenant inferred from key)
#   - All db_service.py calls automatically go to the right tenant DB

import os
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from app.routes.review import router as review_router
from app.routes.assignment_review import router as assignment_router
from app.tenants import resolve_tenant_by_key, all_tenant_ids, configured_tenant_ids, TENANTS, Tenant
from app.database import test_all_tenants, set_current_tenant

app = FastAPI(
    title="Upskillize AI Review Agent",
    description="Multi-tenant AI evaluation for case studies and assignments",
    version="3.0.0",
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
    allow_headers=["Content-Type", "x-api-key"],   # X-Tenant-Id no longer needed
)


# ===== Auth + tenant resolution dependency =====
def require_auth_and_tenant(x_api_key: str = Header(default="")) -> Tenant:
    """
    Single dependency that:
      1. Validates x-api-key by looking up which tenant owns it
      2. Sets the request-scoped tenant context for db queries
      3. Returns the Tenant for routes that want it explicitly

    Tenant identity comes from the key — no other header needed.
    """
    tenant = resolve_tenant_by_key(x_api_key)
    set_current_tenant(tenant)
    return tenant


# ===== Routes =====
app.include_router(review_router, dependencies=[Depends(require_auth_and_tenant)])
app.include_router(assignment_router, dependencies=[Depends(require_auth_and_tenant)])


# ===== Public endpoints (no auth) =====

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "agent": "upskillize-multi-tenant-reviewer",
        "version": "3.0.0",
        "aiProvider": os.getenv("AI_PROVIDER", "huggingface"),
        "tenants": all_tenant_ids(),
        "tenantsConfigured": configured_tenant_ids(),
    }


@app.get("/api/tenants")
async def list_tenants():
    """Public list of tenants this agent serves (no auth, no DB credentials exposed)."""
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
    print("🚀 Upskillize AiRev Agent v3.0 (Multi-Tenant — per-tenant keys)")
    print(f"   AI Provider     : {os.getenv('AI_PROVIDER', 'huggingface')}")
    print(f"   Allowed Origins : {ALLOWED_ORIGINS}")
    print(f"   Registered      : {all_tenant_ids()}")
    configured = configured_tenant_ids()
    print(f"   Configured      : {configured}")
    print(f"   Web UI          : Visit / for the standalone frontend")
    print("")
    if not configured:
        print("   ⚠️  NO TENANTS CONFIGURED. Set <TENANT>_API_KEY and <TENANT>_DATABASE_URL")
        print("       env vars on the HF Space, then restart.")
        print("")
        return
    print("   Testing tenant database connections...")
    results = test_all_tenants()
    ok = sum(1 for v in results.values() if v)
    print(f"   {ok}/{len(results)} tenant DBs connected")
    print("")


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 7860))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)