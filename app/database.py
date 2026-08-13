# app/database.py
# Multi-tenant DB access using request-scoped context.
# Auth dependency sets the tenant; all query()/execute() calls read it
# from contextvar — keeps the existing db_service.py untouched.
#
# PERF (21 Jul 2026): connection POOLING added. Previously every query()
# opened a fresh TCP+TLS+auth handshake to Aiven (~1-2s each); one AiRev
# hub page load ran 10-15 of them → 20-30s loads. Connections are now
# borrowed from a per-database pool and reused. autocommit=True so a
# pooled connection never carries a stale REPEATABLE-READ snapshot
# between borrowers. canonical_student_id is cached (10-min TTL,
# tenant-scoped) — it was re-resolving on every request.

import os
import queue
import threading
import time
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
# Confirmed 19 Jul by reading both codebases. The original lookup therefore
# matched the given id AND both mappings — bidirectional.
#
# THAT IS A DATA LEAK. The two id ranges OVERLAP, so the bidirectional match
# resolves one learner's id into other learners' rows. Measured against
# production on 13 Aug 2026:
#
#   1,435 submissions were returnable for two different learners
#   378 of 503 distinct student_id values were ambiguous
#   a lookup for 937 expanded to IN (937, 1178, 827) and returned the work of
#   THREE people: student 937, student 1178, and student 827 (user_id 937)
#
# Enrolment cannot disambiguate them — 1,449 rows have both candidates enrolled
# in the same course, because the cohort shares courses.
#
# What the data DOES say: of 2,087 submissions only TWO carry a student_id that
# is not a valid students.id. The column is students.id in practice. So the
# correct lookup is a single exact match, and the reverse hops are what break
# it.
#
# STRICT_STUDENT_ID=1 switches to that single match. Each learner resolves to
# exactly one canonical id and canonical ids are injective across real users,
# so every row is returned to at most ONE learner — the bleed is closed by
# construction, not narrowed. Cost: ~2 rows stored under a users.id stop being
# visible to their owner. The rows are untouched; unset the variable and they
# return.
#
# Default is OFF: deploying this changes nothing until the variable is set.
#
# CAVEAT — do NOT run tools/bulk_review.py with this ON yet. It reads
# student_id straight from the database (a students.id) and passes it to the
# route, which feeds it to canonical_student_id() — and that maps
# user_id -> id, so a students.id that collides with another learner's user_id
# resolves to the wrong person. Panel traffic is unaffected (it sends users.id,
# which is what canonical_student_id expects). Making the id space explicit at
# the route boundary is the follow-up that clears this.
#
# BOTH forms take THREE params: (sid, sid, sid). Identical arity, so no call
# site changes — the strict form simply binds the same id three times.
STRICT_STUDENT_ID = os.getenv("STRICT_STUDENT_ID", "").strip().lower() in {
    "1", "true", "yes", "on"}

DUAL_ID_MATCH = (
    "student_id IN (%s, %s, %s)"          # strict: all three bind the same id
    if STRICT_STUDENT_ID else
    "student_id IN (%s, "
    "COALESCE((SELECT user_id FROM students WHERE id = %s LIMIT 1), -1), "
    "COALESCE((SELECT id FROM students WHERE user_id = %s LIMIT 1), -1))")


# ---------- canonical_student_id (cached) ----------------------------------

_SID_TTL_SECONDS = 600          # student→user mapping is near-static; 10 min
_SID_CACHE_MAX = 5000           # hard cap — clear rather than grow unbounded
_sid_cache: dict = {}           # (tenant_id, given) -> (resolved, expires_at)


def canonical_student_id(given: int, id_space: Optional[str] = None) -> int:
    """Normalize an incoming student identifier to students.id — the form
    the Coursework module writes and the canonical id for ALL AiRev reads
    and writes.

    The AiRev panel is mounted with users.id (frontend line:
    `<AiRevPanel studentId={user.id}>`), so the default maps users.id ->
    students.id via the students table.

    id_space="students" says the caller ALREADY holds a students.id and the
    value must be left alone. That is not a nicety — the mapping query is
    `WHERE user_id = given`, which never checks whether `given` is itself a
    valid students.id. Because the two id ranges overlap, feeding it a
    students.id silently resolves to a DIFFERENT learner: 937 is student 937's
    primary key AND student 827's user_id, so tools/bulk_review.py — which
    reads student_id straight from the table — would have reviewed and graded
    student 827's submission while believing it was 937's. Roughly 1,447 of
    2,089 production rows are ambiguous this way.

    The route cannot infer the id space, so the caller declares it. Anything
    other than "students" behaves exactly as before.

    Fail-open: on any DB error, return the given id unchanged rather than
    blocking a review.

    Cached per (tenant, id, id_space) — the space MUST be part of the key, or
    a lookup from one space serves a cached answer computed for the other."""
    if (id_space or "").strip().lower() == "students":
        return int(given)

    tenant = get_current_tenant()
    cache_key = (tenant.id if tenant else "default", given, id_space or "users")
    hit = _sid_cache.get(cache_key)
    now = time.time()
    if hit and hit[1] > now:
        return hit[0]

    resolved = given
    try:
        rows = query("SELECT id FROM students WHERE user_id = %s LIMIT 1", (given,))
        if rows and rows[0].get("id"):
            resolved = int(rows[0]["id"])
            if resolved != given:
                print(f"ℹ️  student id normalized: users.id {given} -> students.id {resolved}")
        if len(_sid_cache) >= _SID_CACHE_MAX:
            _sid_cache.clear()
        _sid_cache[cache_key] = (resolved, now + _SID_TTL_SECONDS)
    except Exception as e:
        # Fail-open and DON'T cache the failure — next request retries.
        print(f"⚠️ canonical_student_id failed for {given}: {e} — using given id")
    return resolved


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
        # Pool safety: without autocommit a reused connection would hold a
        # REPEATABLE-READ snapshot from its previous borrower and serve
        # stale reads. Explicit commit() in execute paths stays (harmless).
        "autocommit": True,
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


# ---------- Connection pool ------------------------------------------------
# One pool per database URL (i.e. per tenant). queue.Queue is thread-safe;
# a borrowed connection is exclusively owned until released, which matches
# the sync-handler-in-threadpool execution model. Bursts beyond the pool
# size fall back to fresh connections (closed on release when the pool is
# full) — correct under load, never blocking.

_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "8"))
_pools: dict = {}               # db_url -> queue.Queue of idle connections
_pools_lock = threading.Lock()


def _pool_for(url: str) -> queue.Queue:
    pool = _pools.get(url)
    if pool is None:
        with _pools_lock:
            pool = _pools.setdefault(url, queue.Queue(maxsize=_POOL_SIZE))
    return pool


def _borrow(url: str):
    """Reuse an idle pooled connection (ping-revived) or open a fresh one."""
    pool = _pool_for(url)
    while True:
        try:
            conn = pool.get_nowait()
        except queue.Empty:
            return pymysql.connect(**_parse_db_url(url))
        try:
            conn.ping(reconnect=True)   # revives server-side idle timeouts
            return conn
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            # dead connection discarded — try the next pooled one


def _release(url: str, conn, healthy: bool):
    if not healthy:
        try:
            conn.close()
        except Exception:
            pass
        return
    try:
        _pool_for(url).put_nowait(conn)
    except queue.Full:
        try:
            conn.close()
        except Exception:
            pass


def _run(url: str, sql: str, params: tuple, commit: bool):
    """Single execution path for all four public helpers — pooled."""
    conn = _borrow(url)
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            if commit:
                conn.commit()
                result = cursor.lastrowid
            else:
                result = cursor.fetchall()
    except Exception:
        _release(url, conn, healthy=False)
        raise
    _release(url, conn, healthy=True)
    return result


# ---------- Explicit tenant primitives ------------------------------------

def get_tenant_connection(tenant: Tenant):
    """Fresh, unpooled connection — caller owns and closes it.
    Prefer tquery()/texecute(), which pool."""
    return pymysql.connect(**_parse_db_url(tenant.database_url))


def tquery(tenant: Tenant, sql: str, params: tuple = ()):
    try:
        return _run(tenant.database_url, sql, params, commit=False)
    except Exception as e:
        print(f"❌ DB error (tenant={tenant.id}): {e}")
        raise


def texecute(tenant: Tenant, sql: str, params: tuple = ()):
    try:
        return _run(tenant.database_url, sql, params, commit=True)
    except Exception as e:
        print(f"❌ DB error (tenant={tenant.id}): {e}")
        raise


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
    """Fresh, unpooled connection — caller owns and closes it.
    Prefer query()/execute(), which pool."""
    return pymysql.connect(**_parse_db_url(_resolve_url()))


def query(sql: str, params: tuple = ()):
    return _run(_resolve_url(), sql, params, commit=False)


def execute(sql: str, params: tuple = ()):
    return _run(_resolve_url(), sql, params, commit=True)


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
