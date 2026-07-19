# app/database.py
# Multi-tenant DB access using request-scoped context.
# Auth dependency sets the tenant; all query()/execute() calls read it
# from contextvar — keeps the existing db_service.py untouched.

import os
import contextvars
import pymysql
import pymysql.cursors
from urllib.parse import urlparse, unquote
from typing import Optional

from app.tenants import Tenant, TENANTS, all_tenant_ids


_current_tenant: contextvars.ContextVar[Optional[Tenant]] = contextvars.ContextVar(
    "current_tenant", default=None
)


def set_current_tenant(tenant: Tenant):
    """Called by the FastAPI auth dependency once per request."""
    _current_tenant.set(tenant)


# The LMS carries TWO student identities and BOTH appear in submission rows:
# the AiRev panel is mounted with users.id (frontend: studentId={user.id})
# while the Coursework module writes students.id (via resolveStudent).
# Confirmed 19 Jul by reading both codebases. Any submission lookup must
# therefore match the given id AND both mappings — bidirectional:
#   given users.id    -> students.id via (SELECT id  ... WHERE user_id = ?)
#   given students.id -> users.id    via (SELECT user_id ... WHERE id  = ?)
# ONE definition, imported everywhere. Takes THREE params: (sid, sid, sid).
DUAL_ID_MATCH = (
    "student_id IN (%s, "
    "COALESCE((SELECT user_id FROM students WHERE id = %s LIMIT 1), -1), "
    "COALESCE((SELECT id FROM students WHERE user_id = %s LIMIT 1), -1))")


def canonical_student_id(given: int) -> int:
    """Normalize an incoming student identifier to students.id — the form
    the Coursework module writes and the canonical id for ALL AiRev reads
    and writes.

    The AiRev panel is mounted with users.id (frontend line:
    `<AiRevPanel studentId={user.id}>`), so map users.id -> students.id via
    the students table. If no mapping exists, the caller already sent
    students.id (standalone UI, older integrations) — use it as-is.
    Fail-open: on any DB error, return the given id unchanged rather than
    blocking a review. DUAL_ID_MATCH remains on list queries so legacy rows
    written under users.id stay visible."""
    try:
        rows = query("SELECT id FROM students WHERE user_id = %s LIMIT 1", (given,))
        if rows and rows[0].get("id"):
            resolved = int(rows[0]["id"])
            if resolved != given:
                print(f"ℹ️  student id normalized: users.id {given} -> students.id {resolved}")
            return resolved
    except Exception as e:
        print(f"⚠️ canonical_student_id failed for {given}: {e} — using given id")
    return given


def get_current_tenant() -> Optional[Tenant]:
    return _current_tenant.get()


def _parse_db_url(db_url: str) -> dict:
    clean_url = db_url.replace("mysql+pymysql://", "mysql://")
    clean_url = clean_url.replace("mysql+mysqlconnector://", "mysql://")
    parsed = urlparse(clean_url)
    return {
        "host": parsed.hostname,
        "port": parsed.port or 3306,
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "database": parsed.path.lstrip("/"),
        "cursorclass": pymysql.cursors.DictCursor,
        "connect_timeout": 10,
        "read_timeout": 30,
        "write_timeout": 30,
    }


def _resolve_url() -> str:
    tenant = get_current_tenant()
    if tenant:
        return tenant.database_url

    # Background tasks fall back to "lms"
    if "lms" in TENANTS:
        try:
            return TENANTS["lms"].database_url
        except RuntimeError:
            pass

    url = os.getenv("DATABASE_URL", "")
    if not url:
        raise RuntimeError(
            "No tenant context AND no DATABASE_URL env var. "
            "Send X-Tenant-Id header on the request."
        )
    return url


# ---------- Explicit tenant primitives ------------------------------------

def get_tenant_connection(tenant: Tenant):
    return pymysql.connect(**_parse_db_url(tenant.database_url))


def tquery(tenant: Tenant, sql: str, params: tuple = ()):
    conn = get_tenant_connection(tenant)
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall()
    except Exception as e:
        print(f"❌ DB error (tenant={tenant.id}): {e}")
        raise
    finally:
        conn.close()


def texecute(tenant: Tenant, sql: str, params: tuple = ()):
    conn = get_tenant_connection(tenant)
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            conn.commit()
            return cursor.lastrowid
    except Exception as e:
        print(f"❌ DB error (tenant={tenant.id}): {e}")
        raise
    finally:
        conn.close()


def test_tenant_connection(tenant: Tenant) -> bool:
    try:
        tquery(tenant, "SELECT 1 as connected")
        return True
    except Exception as e:
        print(f"   ❌ tenant '{tenant.id}' connection FAILED: {e}")
        return False


def test_all_tenants() -> dict:
    results = {}
    for tid in all_tenant_ids():
        tenant = TENANTS[tid]
        try:
            ok = test_tenant_connection(tenant)
            results[tid] = ok
            if ok:
                print(f"   ✅ tenant '{tid}' ({tenant.label}) connected")
        except RuntimeError as e:
            print(f"   ⚠️  tenant '{tid}' skipped: {e}")
            results[tid] = False
    return results


# ---------- Implicit (context-aware) — used by existing db_service.py ----

def get_connection():
    return pymysql.connect(**_parse_db_url(_resolve_url()))


def query(sql: str, params: tuple = ()):
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall()
    finally:
        conn.close()


def execute(sql: str, params: tuple = ()):
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            conn.commit()
            return cursor.lastrowid
    finally:
        conn.close()


def test_connection() -> bool:
    """Legacy startup hook — tests whatever DATABASE_URL/lms tenant points to."""
    try:
        url = os.getenv("DATABASE_URL", "")
        if not url and "lms" in TENANTS:
            try:
                url = TENANTS["lms"].database_url
            except RuntimeError:
                pass
        if not url:
            return False
        conn = pymysql.connect(**_parse_db_url(url))
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1")
        finally:
            conn.close()
        return True
    except Exception as e:
        print(f"❌ legacy DB ping failed: {e}")
        return False