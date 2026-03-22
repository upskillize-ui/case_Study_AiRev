-- ============================================
-- UPSKILLIZE AI AGENT - DATABASE TABLES
-- ============================================
-- Run these queries on your Avian Cloud MySQL database
-- These ADD new tables — they do NOT modify your existing tables
-- ============================================

-- TABLE 1: Case Studies
-- Stores all case studies with model answers and grading criteria
CREATE TABLE IF NOT EXISTS case_studies (
  id              INT AUTO_INCREMENT PRIMARY KEY,
  course_id       INT NOT NULL,
  title           VARCHAR(500) NOT NULL,
  description     TEXT NOT NULL,
  questions       JSON NOT NULL,
  model_answers   JSON NOT NULL,
  grading_rubric  JSON NOT NULL,
  key_concepts    JSON NOT NULL,
  max_score       INT DEFAULT 100,
  word_limit_min  INT DEFAULT 300,
  word_limit_max  INT DEFAULT 500,
  deadline        DATETIME NULL,
  status          ENUM('draft','published','archived') DEFAULT 'draft',
  created_by      INT NULL,
  created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_course (course_id),
  INDEX idx_status (status)
);

-- TABLE 2: Student Submissions
-- Stores every answer submitted + AI review results + mentor review
CREATE TABLE IF NOT EXISTS case_study_submissions (
  id                  INT AUTO_INCREMENT PRIMARY KEY,
  case_study_id       INT NOT NULL,
  student_id          INT NOT NULL,
  attempt_number      INT DEFAULT 1,
  answer_text         TEXT NOT NULL,
  file_url            VARCHAR(1000) NULL,
  word_count          INT DEFAULT 0,
  submitted_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  status              ENUM('submitted','reviewing','graded','mentor_reviewed') DEFAULT 'submitted',

  -- AI Review Results
  ai_score            DECIMAL(5,2) NULL,
  ai_grade            VARCHAR(10) NULL,
  ai_feedback         JSON NULL,
  ai_rubric_scores    JSON NULL,
  ai_missing_concepts JSON NULL,
  ai_strengths        TEXT NULL,
  ai_improvements     TEXT NULL,
  ai_suggested_modules JSON NULL,
  ai_plagiarism_risk  ENUM('low','medium','high') DEFAULT 'low',
  ai_reviewed_at      TIMESTAMP NULL,

  -- Mentor Review
  mentor_id           INT NULL,
  mentor_score        DECIMAL(5,2) NULL,
  mentor_feedback     TEXT NULL,
  mentor_approved     BOOLEAN DEFAULT FALSE,
  mentor_reviewed_at  TIMESTAMP NULL,

  -- Flags
  is_flagged          BOOLEAN DEFAULT FALSE,
  flag_reason         VARCHAR(500) NULL,

  INDEX idx_student (student_id),
  INDEX idx_case_study (case_study_id),
  INDEX idx_status (status),
  INDEX idx_flagged (is_flagged)
);

-- TABLE 3: Student Performance Tracker
-- Tracks CURRENT score and BEST score for each student per case study
CREATE TABLE IF NOT EXISTS student_performance_tracker (
  id                  INT AUTO_INCREMENT PRIMARY KEY,
  student_id          INT NOT NULL,
  case_study_id       INT NOT NULL,
  current_score       DECIMAL(5,2) DEFAULT 0,
  best_score          DECIMAL(5,2) DEFAULT 0,
  first_attempt_score DECIMAL(5,2) DEFAULT 0,
  total_attempts      INT DEFAULT 0,
  improvement         DECIMAL(5,2) DEFAULT 0,
  last_attempt_at     TIMESTAMP NULL,
  status              ENUM('not_started','in_progress','completed','needs_help') DEFAULT 'not_started',

  UNIQUE KEY unique_student_case (student_id, case_study_id),
  INDEX idx_student (student_id),
  INDEX idx_status (status)
);

-- TABLE 4: Mentor Reports (aggregated)
CREATE TABLE IF NOT EXISTS mentor_reports (
  id                    INT AUTO_INCREMENT PRIMARY KEY,
  case_study_id         INT NOT NULL,
  mentor_id             INT NOT NULL,
  total_submissions     INT DEFAULT 0,
  avg_score             DECIMAL(5,2) DEFAULT 0,
  highest_score         DECIMAL(5,2) DEFAULT 0,
  lowest_score          DECIMAL(5,2) DEFAULT 0,
  students_above_70     INT DEFAULT 0,
  students_below_40     INT DEFAULT 0,
  common_missed_concepts JSON NULL,
  flagged_count         INT DEFAULT 0,
  generated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

  INDEX idx_mentor (mentor_id),
  INDEX idx_case_study (case_study_id)
);

-- TABLE 5: AI Review Logs (for debugging and cost tracking)
CREATE TABLE IF NOT EXISTS ai_review_logs (
  id                INT AUTO_INCREMENT PRIMARY KEY,
  submission_id     INT NOT NULL,
  ai_model_used     VARCHAR(100) NULL,
  prompt_tokens     INT DEFAULT 0,
  response_tokens   INT DEFAULT 0,
  processing_time_ms INT DEFAULT 0,
  raw_ai_response   JSON NULL,
  error_message     TEXT NULL,
  success           BOOLEAN DEFAULT TRUE,
  created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

  INDEX idx_submission (submission_id)
);

-- ============================================
-- SAMPLE DATA (for testing)
-- ============================================
INSERT INTO case_studies (course_id, title, description, questions, model_answers, grading_rubric, key_concepts, word_limit_min, word_limit_max, status)
VALUES (
  1,
  'Digital Lending Revolution in India',
  'XYZ FinTech is a digital lending startup that provides instant personal loans through a mobile app. They use AI-based credit scoring, eKYC verification, and UPI-based disbursement. In 2025, they faced regulatory challenges when RBI introduced new digital lending guidelines. The company needs to adapt its business model while maintaining growth. Their current customer base is 2 million users with an average loan size of Rs. 50,000. They process 10,000 loan applications daily with a 60% approval rate.',
  '["Analyse the impact of RBI digital lending guidelines on FinTech startups like XYZ FinTech. Discuss the challenges, opportunities, and suggest a compliance strategy. (400-500 words)"]',
  '["The ideal answer should cover: 1) RBI Digital Lending Guidelines 2022 overview - key provisions including disclosure requirements, data privacy mandates, and restrictions on third-party lending. 2) Impact on FLDG (First Loss Default Guarantee) model - how the cap on FLDG affects FinTech-NBFC partnerships. 3) eKYC and data privacy requirements - how Video KYC and Aadhaar-based verification must be implemented. 4) Impact on customer acquisition cost - increased compliance costs vs trust building. 5) Opportunities - regulated market creates barriers for unserious players, builds customer trust. 6) Compliance strategy - adopt RegTech solutions, partner with banks rather than NBFCs, implement robust data governance, use AI for automated compliance monitoring."]',
  '{"criteria": [{"name": "Understanding of Concepts", "maxScore": 30, "weight": 0.3}, {"name": "Application and Analysis", "maxScore": 30, "weight": 0.3}, {"name": "Real-World Examples", "maxScore": 20, "weight": 0.2}, {"name": "Structure and Clarity", "maxScore": 20, "weight": 0.2}]}',
  '["RBI Digital Lending Guidelines", "FLDG", "eKYC", "data privacy", "RegTech", "NBFC", "UPI", "credit scoring", "compliance", "digital banking"]',
  300,
  500,
  'published'
);
