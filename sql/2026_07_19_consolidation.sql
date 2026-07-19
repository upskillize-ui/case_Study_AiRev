-- Slice 4: the agent's sleep — nightly consolidation storage.
-- anchor_exemplars: consensus-verified real answers (anonymized excerpts)
--   that future reviews compare against instead of guessing.
-- calibration_notes: what the cohort taught the agent (misconceptions,
--   drift corrections) — injected into review prompts per scope.
-- cohort_stats: score distributions per question for drift detection.
-- agent_config: bounded self-tuned gate values (bounds enforced in code).
-- All auto-create via consolidation_service._ensure_tables(); reference copy.

CREATE TABLE IF NOT EXISTS anchor_exemplars (
    id             INT AUTO_INCREMENT PRIMARY KEY,
    scope_type     VARCHAR(24) NOT NULL,
    scope_id       INT NOT NULL,
    verified_score INT NOT NULL,
    spread         INT NOT NULL,
    excerpt        TEXT NOT NULL,
    reasons        TEXT,
    active         TINYINT DEFAULT 1,
    created_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_scope (scope_type, scope_id, active)
);

CREATE TABLE IF NOT EXISTS calibration_notes (
    id         INT AUTO_INCREMENT PRIMARY KEY,
    scope_type VARCHAR(24) NOT NULL,
    scope_id   INT NOT NULL,
    note       TEXT NOT NULL,
    evidence   TEXT,
    active     TINYINT DEFAULT 1,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_scope (scope_type, scope_id, active)
);

CREATE TABLE IF NOT EXISTS cohort_stats (
    scope_type VARCHAR(24) NOT NULL,
    scope_id   INT NOT NULL,
    n          INT,
    mean_score DECIMAL(5,2),
    std_score  DECIMAL(5,2),
    p25 DECIMAL(5,2), p50 DECIMAL(5,2), p75 DECIMAL(5,2),
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (scope_type, scope_id)
);

CREATE TABLE IF NOT EXISTS agent_config (
    k VARCHAR(64) PRIMARY KEY,
    v VARCHAR(64) NOT NULL,
    evidence TEXT,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);
