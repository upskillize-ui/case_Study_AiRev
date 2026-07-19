-- Slice 1: the agent's long-term memory.
-- One row per (scope_type, scope_id): the crystallized knowledge pack the
-- agent recalls at review time instead of re-reading raw sources.
-- source_hash makes staleness detection automatic: faculty edits change the
-- hash, which triggers a rebuild on next touch.
-- Applied automatically by knowledge_service._ensure_table(); kept here for
-- reference and manual runs.

CREATE TABLE IF NOT EXISTS agent_knowledge (
    id             INT AUTO_INCREMENT PRIMARY KEY,
    scope_type     VARCHAR(24)  NOT NULL,   -- case_study | assignment | capstone | industry_session
    scope_id       INT          NOT NULL,
    source_hash    CHAR(32)     NOT NULL,   -- md5 of the raw sources the pack was built from
    knowledge_json LONGTEXT     NOT NULL,   -- the pack (concepts, band anchors, misconceptions, ...)
    status         VARCHAR(16)  NOT NULL DEFAULT 'ready',  -- building | ready | failed
    version        INT          NOT NULL DEFAULT 1,
    model          VARCHAR(128),
    error          TEXT,
    created_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at     DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_scope (scope_type, scope_id)
);
