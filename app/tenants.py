# app/tenants.py
# ---------------------------------------------------------------------------
# Multi-tenant configuration — Option C: per-tenant API keys.
#
# The agent runs as a single HuggingFace Space serving multiple LMS tenants
# (Upskillize main LMS, eaprep, future white-labels). Each tenant has:
#   - Its own MySQL database on Aiven
#   - Its own API key (frontend sends as x-api-key header)
#
# How it works:
#   1. Frontend sends its tenant's key in the x-api-key header
#   2. Agent looks up which tenant that key belongs to (resolve_tenant_by_key)
#   3. All DB queries route to that tenant's DB
#
# Security wins vs. single shared key:
#   - One leaked key only exposes ONE tenant
#   - Rotate per-tenant without affecting others
#   - Audit logs show exactly which tenant made which call
#   - No X-Tenant-Id header needed (one fewer thing for clients to get wrong)
#
# How to add a new tenant:
#   1. Create the new database on Aiven
#   2. Run the existing schema migrations against it
#   3. Generate a fresh API key (PowerShell command in setup guide)
#   4. Add an entry below pointing to the env vars for its DB URL + API key
#   5. Set both env vars as HF Space secrets
#   6. Add the tenant's frontend origin to ALLOWED_ORIGINS env
#   7. On the new tenant's Netlify, set VITE_AGENT_API_KEY to its key
# ---------------------------------------------------------------------------

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


class Tenant:
    """A single tenant's runtime config."""

    __slots__ = ("id", "name", "database_url_env", "api_key_env", "label")

    def __init__(
        self,
        id: str,
        name: str,
        database_url_env: str,
        api_key_env: str,
        label: str = "",
    ):
        self.id = id
        self.name = name
        self.database_url_env = database_url_env
        self.api_key_env = api_key_env
        self.label = label or name

    @property
    def database_url(self) -> str:
        # .strip(): pasting into a web secrets field very easily leaves a
        # trailing newline or space. An invisible character here becomes an
        # unexplainable connection failure.
        url = os.getenv(self.database_url_env, "").strip()
        if not url:
            raise RuntimeError(
                f"Tenant '{self.id}' is configured but env var "
                f"'{self.database_url_env}' is not set on the HF Space."
            )
        return url

    @property
    def api_key(self) -> str:
        # .strip() — MUST match resolve_tenant_by_key(), which strips the
        # INCOMING header. Without it the comparison is asymmetric: a stored
        # key with one trailing newline never equals the header the browser
        # sends, and EVERY request 401s with no clue why. That is exactly what
        # happened on 13 Aug after a secret was re-pasted: agent healthy,
        # tenants "configured: true", every student locked out.
        key = os.getenv(self.api_key_env, "").strip()
        if not key:
            raise RuntimeError(
                f"Tenant '{self.id}' is configured but env var "
                f"'{self.api_key_env}' is not set on the HF Space."
            )
        return key

    def has_api_key(self) -> bool:
        """True if this tenant's API key env var is set."""
        return bool(os.getenv(self.api_key_env, ""))


# ---------------------------------------------------------------------------
# Registered tenants
# ---------------------------------------------------------------------------
TENANTS: dict[str, Tenant] = {
    "lms": Tenant(
        id="lms",
        name="Upskillize LMS",
        database_url_env="DATABASE_URL",          # legacy main DB var
        api_key_env="LMS_API_KEY",                # NEW: per-tenant key
        label="lms.upskillize.com",
    ),
    "eaprep": Tenant(
        id="eaprep",
        name="EA Prep",
        database_url_env="EAPREP_DATABASE_URL",
        api_key_env="EAPREP_API_KEY",
        label="eaprep.upskillize.com",
    ),
    # Future white-labels — uncomment and fill in:
    # "acme": Tenant(
    #     id="acme",
    #     name="ACME University",
    #     database_url_env="ACME_DATABASE_URL",
    #     api_key_env="ACME_API_KEY",
    #     label="lms.acme.edu",
    # ),
}


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------

def resolve_tenant_by_key(api_key: str) -> Tenant:
    """
    Look up a tenant by API key. The single source of truth for "who is calling".
    Raises HTTPException(401) if the key matches no tenant.

    Used by the FastAPI auth dependency in main.py — every authenticated
    request has its tenant resolved here.
    """
    from fastapi import HTTPException

    # A 401 in the access log is a dead end: "missing" and "wrong" need
    # completely different fixes — one is a caller that forgot the header, the
    # other is a stale key in a frontend build — and the log said neither.
    # Live on 31 Aug: every student screen for one learner 401ing, with nothing
    # to distinguish "the header never arrived" from "the header is out of date".
    #
    # The LENGTH is logged and the key never is. A truncated or empty value
    # shows itself immediately, and a length that matches while the key does not
    # points straight at a rotated secret.
    if not api_key:
        logger.warning("[tenant] 401 — no x-api-key header on the request. The "
                       "caller is not sending it at all.")
        raise HTTPException(status_code=401, detail="missing x-api-key header")

    api_key = api_key.strip()
    for tenant in TENANTS.values():
        try:
            # Constant-time comparison would be ideal here. For now, equality
            # is fine — these aren't user passwords, and timing attacks
            # would have to be very precise to extract a 48-char random key.
            if tenant.api_key == api_key:
                return tenant
        except RuntimeError:
            # api_key env var not set for this tenant — skip it
            continue

    configured = [t.id for t in TENANTS.values() if t.has_api_key()]
    logger.warning(
        "[tenant] 401 — x-api-key of length %d matches no tenant. Configured "
        "here: %s. If the length looks right, the key has been rotated on one "
        "side only: compare the caller's build-time key with LMS_API_KEY on "
        "this Space.", len(api_key), configured or "NONE")
    raise HTTPException(status_code=401, detail="invalid x-api-key")


def all_tenant_ids() -> list[str]:
    """For startup logging."""
    return sorted(TENANTS.keys())


def configured_tenant_ids() -> list[str]:
    """Tenants whose api_key + database_url env vars are both set."""
    out = []
    for tid, tenant in TENANTS.items():
        if not tenant.has_api_key():
            continue
        if not os.getenv(tenant.database_url_env):
            continue
        out.append(tid)
    return sorted(out)