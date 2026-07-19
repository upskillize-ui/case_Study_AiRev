-- Slice 2: the agent's reflexes.
-- submission_fingerprints: every submission leaves a normalized SHA-256 so
-- cohort-wide copy detection is an O(1) indexed lookup, never a scan.
-- exception_queue: the human exception list — abuse, cohort duplicates,
-- ~90%+ AI authorship, garbage, disputes. Mentors review dozens of flagged
-- items per cohort instead of thousands of submissions.
-- Both tables auto-create via prefilter_service._ensure_tables(); this file
-- is the reference copy for manual runs.

CREATE TABLE IF NOT EXISTS submission_fingerprints (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    scope_type    VARCHAR(24) NOT NULL,
    scope_id      INT         NOT NULL,
    student_id    INT         NOT NULL,
    submission_id INT         NULL,
    text_hash     CHAR(64)    NOT NULL,
    word_count    INT         DEFAULT 0,
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_scope_hash (scope_type, scope_id, text_hash)
);

CREATE TABLE IF NOT EXISTS exception_queue (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    scope_type      VARCHAR(24) NOT NULL,
    scope_id        INT         NOT NULL,
    student_id      INT         NOT NULL,
    submission_id   INT         NULL,
    reason          VARCHAR(32) NOT NULL,
    detail          TEXT,
    status          VARCHAR(16) NOT NULL DEFAULT 'open',
    resolved_by     VARCHAR(128),
    resolution_note TEXT,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    resolved_at     DATETIME NULL,
    INDEX idx_status (status, scope_type)
);
