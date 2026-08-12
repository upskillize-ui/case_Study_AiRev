-- Adaptive per-task rubrics (rubric_service).
-- Created automatically at first use per tenant; this file exists so
-- run_migrations.py can provision it explicitly too.
CREATE TABLE IF NOT EXISTS derived_rubrics (
    scope_type   VARCHAR(32)  NOT NULL,
    scope_id     INT          NOT NULL,
    source_hash  CHAR(32)     NOT NULL,
    payload      LONGTEXT     NOT NULL,
    created_at   DATETIME     DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (scope_type, scope_id)
);
