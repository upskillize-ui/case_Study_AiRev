# app/services/knowledge_service.py
# The agent's long-term memory: read the faculty sources ONCE, crystallize a
# knowledge pack, recall it on every review. "Read once, remember forever."
#
# Design (per the brain spec):
#   - One builder per scope_type in a strategy registry — adding a review type
#     means adding a registry entry, never an if/elif ladder.
#   - Staleness is a content hash: faculty edits change the hash, which makes
#     the pack stale and triggers a rebuild on next touch. Fully automatic —
#     no faculty action, no webhook required (a webhook can still force it).
#   - Missing pack  -> built synchronously (one-time cost, first review waits
#     a few seconds so it is never reviewed against nothing).
#   - Stale pack    -> previous version is used NOW, rebuild runs in the
#     background so no student ever waits on a refresh.
#
# The pack is the ONLY thing reviews recall. Raw sources are never re-sent to
# the model at review time — that is where the token savings live.

import hashlib
import json
from typing import Callable, Optional

from app.database import query, execute
from app.services import ai_service

_TABLE = "agent_knowledge"
_table_ready = False

# Pack schema shared by all builders. Kept flat and compact on purpose: this
# whole object is injected into every review prompt, so every field must earn
# its tokens.
PACK_SCHEMA = {
    "type": "object",
    "properties": {
        "concepts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name":         {"type": "string"},
                    "why_it_matters": {"type": "string"},
                    "must_cover":   {"type": "boolean"},
                },
                "required": ["name", "why_it_matters", "must_cover"],
            },
        },
        "question_demands":      {"type": "array", "items": {"type": "string"}},
        "ideal_answer_skeleton": {"type": "array", "items": {"type": "string"}},
        "band_anchors": {
            "type": "object",
            "properties": {
                "outstanding": {"type": "string"},
                "proficient":  {"type": "string"},
                "emerging":    {"type": "string"},
            },
            "required": ["outstanding", "proficient", "emerging"],
        },
        "common_misconceptions": {"type": "array", "items": {"type": "string"}},
        "specificity_markers":   {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "concepts", "question_demands", "ideal_answer_skeleton",
        "band_anchors", "common_misconceptions", "specificity_markers",
    ],
}

_BUILD_INSTRUCTIONS = """You are AiRev's knowledge builder. Study the faculty material below ONCE and crystallize it into an evaluation pack that future reviews will recall instead of re-reading the sources.

Rules:
- concepts: the 5-10 ideas a correct answer must engage. must_cover=true only for genuinely essential ones.
- question_demands: what each question actually asks the student to DO (analyse, recommend, compare...).
- ideal_answer_skeleton: the key POINTS of a top answer — points, never prose paragraphs.
- band_anchors: one sentence each describing what an outstanding / proficient / emerging answer looks like for THIS material.
- common_misconceptions: mistakes students plausibly make here.
- specificity_markers: the concrete facts, figures, names and constraints from THIS material that a grounded answer would reference. These power the case-specificity gate — choose markers a generic essay would never contain.
- Derive everything from the material alone. Invent nothing."""


def _ensure_table() -> None:
    global _table_ready
    if _table_ready:
        return
    execute("""
        CREATE TABLE IF NOT EXISTS agent_knowledge (
            id             INT AUTO_INCREMENT PRIMARY KEY,
            scope_type     VARCHAR(24)  NOT NULL,
            scope_id       INT          NOT NULL,
            source_hash    CHAR(32)     NOT NULL,
            knowledge_json LONGTEXT     NOT NULL,
            status         VARCHAR(16)  NOT NULL DEFAULT 'ready',
            version        INT          NOT NULL DEFAULT 1,
            model          VARCHAR(128),
            error          TEXT,
            created_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at     DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uq_scope (scope_type, scope_id)
        )
    """)
    _table_ready = True


def source_hash(sources: dict) -> str:
    """Deterministic fingerprint of the raw faculty material."""
    canonical = json.dumps(sources, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.md5(canonical.encode("utf-8")).hexdigest()


# ─── Source assembly — one function per scope type (strategy registry) ────────

def _case_study_sources(cs: dict) -> dict:
    return {
        "title":        cs.get("title", ""),
        "description":  cs.get("description", ""),
        "questions":    cs.get("questions", []),
        "model_answer": cs.get("modelAnswers", ""),
        "rubric":       cs.get("gradingRubric", {}),
        "key_concepts": cs.get("keyConcepts", []),
    }


def _assignment_sources(a: dict) -> dict:
    # Assignment rows mirror the case-study shape (see assignment_db_service).
    return {
        "title":        a.get("title", ""),
        "brief":        a.get("description", "") or a.get("brief", ""),
        "questions":    a.get("questions", []),
        "model_answer": a.get("modelAnswers", ""),
        "rubric":       a.get("gradingRubric", {}) or a.get("rubric", {}),
        "key_concepts": a.get("keyConcepts", []),
    }


def _capstone_sources(c: dict) -> dict:
    # Capstone rows are leaner: title/description (+ synthesized rubric passed
    # in by the route). The builder still extracts concepts and specificity
    # markers from the brief itself.
    return {
        "title":      c.get("title", "") or "Capstone Project",
        "brief":      c.get("description", "") or c.get("brief", ""),
        "milestones": c.get("milestones", []),
        "rubric":     c.get("gradingRubric", {}) or c.get("rubric", {}),
    }


def _session_sources(s: dict) -> dict:
    # Slice 1 uses whatever text the session row already has. Slice 3 (media
    # pipeline) upgrades `transcript` to the full cleaned video transcript —
    # same builder, richer source, automatic rebuild via the changed hash.
    return {
        "title":      s.get("title", ""),
        "mentor":     s.get("mentor", ""),
        "description": s.get("description", ""),
        "key_topics": s.get("key_topics", []),
        "outline":    s.get("outline", ""),
        "outcomes":   s.get("outcomes", ""),
        "transcript": s.get("transcript", ""),
    }


SOURCE_BUILDERS: dict[str, Callable[[dict], dict]] = {
    "case_study":       _case_study_sources,
    "assignment":       _assignment_sources,
    "capstone":         _capstone_sources,
    "industry_session": _session_sources,
}


# ─── Core API ────────────────────────────────────────────────────────────────

def get_pack(scope_type: str, scope_id: int) -> Optional[dict]:
    """Return the stored pack row, or None. Never builds."""
    _ensure_table()
    rows = query(
        f"SELECT source_hash, knowledge_json, status, version FROM {_TABLE} "
        f"WHERE scope_type=%s AND scope_id=%s LIMIT 1",
        (scope_type, scope_id),
    )
    if not rows or rows[0]["status"] != "ready":
        return None
    row = rows[0]
    try:
        pack = json.loads(row["knowledge_json"])
    except (TypeError, ValueError):
        return None
    return {"pack": pack, "source_hash": row["source_hash"], "version": row["version"]}


def get_or_build(scope_type: str, scope_id: int, raw: dict,
                 background_tasks=None) -> Optional[dict]:
    """The recall path every review calls.

    Fresh pack -> returned immediately.
    Stale pack -> previous version returned now; rebuild scheduled in the
                  background (students never wait on a refresh).
    No pack    -> built synchronously so the very first review is never
                  scored against nothing.
    Returns {"pack", "version"} or None if a synchronous build failed
    (caller falls back to legacy raw-source review).
    """
    if scope_type not in SOURCE_BUILDERS:
        raise ValueError(f"Unknown scope_type '{scope_type}'")

    sources = SOURCE_BUILDERS[scope_type](raw)
    fresh_hash = source_hash(sources)
    stored = get_pack(scope_type, scope_id)

    if stored and stored["source_hash"] == fresh_hash:
        return {"pack": stored["pack"], "version": stored["version"]}

    if stored:  # stale — serve old, rebuild behind the scenes
        if background_tasks is not None:
            background_tasks.add_task(build_pack, scope_type, scope_id, sources, fresh_hash)
        else:
            print(f"ℹ️  Pack stale for {scope_type}:{scope_id}, no background runner — serving old version")
        return {"pack": stored["pack"], "version": stored["version"]}

    return build_pack(scope_type, scope_id, sources, fresh_hash)


def build_pack(scope_type: str, scope_id: int, sources: dict,
               fresh_hash: str) -> Optional[dict]:
    """Study the sources once and persist the crystallized pack."""
    _ensure_table()
    _upsert(scope_type, scope_id, fresh_hash, "{}", status="building")
    try:
        prompt = (
            f"{_BUILD_INSTRUCTIONS}\n\n"
            f"SCOPE: {scope_type}\n"
            f"=== FACULTY MATERIAL ===\n"
            f"{json.dumps(sources, ensure_ascii=False, default=str)}"
        )
        pack = ai_service.call_structured(
            blocks=[{"text": prompt, "cache": False}],
            schema=PACK_SCHEMA,
            tier="strong",          # knowledge is built once — use the better brain
            max_tokens=3000,
        )
        version = _upsert(scope_type, scope_id, fresh_hash,
                          json.dumps(pack, ensure_ascii=False), status="ready")
        print(f"🧠 Knowledge pack built: {scope_type}:{scope_id} v{version} "
              f"({len(pack.get('concepts', []))} concepts)")
        return {"pack": pack, "version": version}
    except Exception as e:
        print(f"❌ Pack build failed for {scope_type}:{scope_id}: {e}")
        execute(
            f"UPDATE {_TABLE} SET status='failed', error=%s "
            f"WHERE scope_type=%s AND scope_id=%s",
            (str(e)[:1000], scope_type, scope_id),
        )
        return None


def _upsert(scope_type: str, scope_id: int, fresh_hash: str,
            knowledge_json: str, status: str) -> int:
    """Insert or update the single row for this scope. Returns new version."""
    rows = query(
        f"SELECT id, version FROM {_TABLE} WHERE scope_type=%s AND scope_id=%s LIMIT 1",
        (scope_type, scope_id),
    )
    if rows:
        new_version = rows[0]["version"] + (1 if status == "ready" else 0)
        execute(
            f"UPDATE {_TABLE} SET source_hash=%s, knowledge_json=%s, status=%s, "
            f"version=%s, error=NULL WHERE id=%s",
            (fresh_hash, knowledge_json, status, new_version, rows[0]["id"]),
        )
        return new_version
    execute(
        f"INSERT INTO {_TABLE} (scope_type, scope_id, source_hash, knowledge_json, status) "
        f"VALUES (%s, %s, %s, %s, %s)",
        (scope_type, scope_id, fresh_hash, knowledge_json, status),
    )
    return 1


def render_for_prompt(pack: dict) -> str:
    """Compact text rendering of a pack for injection into review prompts."""
    lines = ["=== AGENT KNOWLEDGE (crystallized from faculty material — your ground truth) ==="]
    lines.append("CONCEPTS a correct answer must engage:")
    for c in pack.get("concepts", []):
        flag = " [MUST]" if c.get("must_cover") else ""
        lines.append(f"- {c['name']}{flag}: {c.get('why_it_matters', '')}")
    if pack.get("question_demands"):
        lines.append("WHAT THE QUESTIONS DEMAND: " + " | ".join(pack["question_demands"]))
    if pack.get("ideal_answer_skeleton"):
        lines.append("IDEAL ANSWER COVERS: " + " | ".join(pack["ideal_answer_skeleton"]))
    ba = pack.get("band_anchors", {})
    if ba:
        lines.append(f"BANDS — Outstanding: {ba.get('outstanding','')} · "
                     f"Proficient: {ba.get('proficient','')} · Emerging: {ba.get('emerging','')}")
    if pack.get("common_misconceptions"):
        lines.append("WATCH FOR MISCONCEPTIONS: " + " | ".join(pack["common_misconceptions"]))
    if pack.get("specificity_markers"):
        lines.append("SPECIFICITY MARKERS (a grounded answer references these): "
                     + " | ".join(pack["specificity_markers"]))
    return "\n".join(lines)
