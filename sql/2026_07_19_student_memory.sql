-- Slice 5: person-memory. One rolling profile per student — last 12 review
-- outcomes, recurring weaknesses, trajectory, authorship history. Used for
-- feedback continuity and stylometry trending; never for scoring.
-- Auto-creates via student_memory_service._ensure_table(); reference copy.

CREATE TABLE IF NOT EXISTS student_memory (
    student_id   INT PRIMARY KEY,
    profile_json LONGTEXT NOT NULL,
    updated_at   DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);
