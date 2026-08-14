# app/routes/exceptions.py
# Mentor exception queue — the scalable replacement for manual review at
# 2-3k students/batch. Humans see ~dozens of flagged items per cohort
# (abuse, cohort duplicates, ~90%+ AI authorship, garbage, disputes)
# instead of every submission. Thin endpoints; logic in prefilter_service.

from app.auth import require_admin
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional

from app.services import prefilter_service

router = APIRouter(prefix="/api/review", tags=["exceptions"])


class ResolveRequest(BaseModel):
    resolvedBy: str
    note: Optional[str] = ""
    dismiss: bool = False


# STAFF ONLY — the academic-integrity queue. Open, it told the cohort who
# had been flagged for AI authorship and who was paired in a duplicate.
@router.get("/exceptions", dependencies=[Depends(require_admin)])
async def list_exceptions(status: str = "open", limit: int = 100):
    rows = prefilter_service.list_exceptions(status=status, limit=limit)
    return {
        "success": True,
        "count": len(rows),
        "exceptions": [
            {
                "id":           r["id"],
                "scopeType":    r["scope_type"],
                "scopeId":      r["scope_id"],
                "studentId":    r["student_id"],
                "submissionId": r.get("submission_id"),
                "reason":       r["reason"],
                "detail":       r.get("detail"),
                "status":       r["status"],
                "createdAt":    str(r["created_at"]) if r.get("created_at") else None,
            }
            for r in rows
        ],
    }


# STAFF ONLY — resolvedBy is caller-supplied text, so open, a flagged learner
# could dismiss their own flag under a forged reviewer name.
@router.post("/exceptions/{exception_id}/resolve",
             dependencies=[Depends(require_admin)])
async def resolve_exception(exception_id: int, req: ResolveRequest):
    ok = prefilter_service.resolve_exception(
        exception_id, req.resolvedBy, req.note or "", dismiss=req.dismiss)
    if not ok:
        raise HTTPException(status_code=404, detail="Exception not found or already handled")
    return {"success": True, "id": exception_id,
            "status": "dismissed" if req.dismiss else "resolved"}
