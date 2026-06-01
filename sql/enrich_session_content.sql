-- ============================================================
-- Enrich industry_sessions schema with full content fields
-- so the AI can do deep, evidence-based reviews
-- ============================================================

USE defaultdb;

-- Add rich content columns (only if missing)
ALTER TABLE industry_sessions ADD COLUMN examples_discussed TEXT NULL
  COMMENT 'Real-world examples mentor used during the session';
ALTER TABLE industry_sessions ADD COLUMN case_studies TEXT NULL
  COMMENT 'Case studies or named companies discussed';
ALTER TABLE industry_sessions ADD COLUMN key_quotes TEXT NULL
  COMMENT 'Memorable quotes / one-liners from the mentor';
ALTER TABLE industry_sessions ADD COLUMN assignments_given TEXT NULL
  COMMENT 'Tasks / homework assigned during session';
ALTER TABLE industry_sessions ADD COLUMN resources_shared TEXT NULL
  COMMENT 'Books, reports, links the mentor shared';
ALTER TABLE industry_sessions ADD COLUMN learning_outcomes TEXT NULL
  COMMENT 'What students were expected to walk away knowing';

-- ============================================================
-- TEMPLATE: enrich the FinTech session with FULL content
-- (edit values to match your actual session)
-- ============================================================
UPDATE industry_sessions
SET
  examples_discussed = 'HDFC Bank''s fraud detection AI rollout — replaced rule-based system in 2024, cut false positives 40%
SBI''s core banking modernization with TCS BaNCS — 5-year migration, ₹2,000 cr budget
ICICI''s real-time payment fraud detection using graph neural networks
Kotak Mahindra moving from in-house RegTech to vendor-sourced (Signzy partnership)',

  case_studies = 'Perfios — won 6 of top 10 Indian banks for income verification by being the only player with audited 5-year P&L
Setu — sold API infrastructure to banks (not apps), now powers ~30% of NBFC partnerships
Signzy — pivoted from selling KYC SaaS to white-label embedded compliance, doubled ARR in 18 months
M2P Fintech — card issuance infrastructure, now profitable on revenue-share with banks instead of per-card fee',

  key_quotes = '"Banks aren''t buying innovation anymore — they''re buying audited financial statements and SLAs."
"The CDO''s budget is dead. Talk to the CRO or COO if you want a real check."
"If your FinTech can''t pass a 6-month due diligence, you''re not in the conversation."
"Revenue share is the new licensing — banks want skin in the game, not a software bill."',

  assignments_given = 'Pick one of: Perfios, Setu, Signzy, M2P. Write a 1-page memo on:
  (a) what specific bank pain point they solve
  (b) why they win where others fail
  (c) what would kill them in the next 18 months
Submit via Coursework > Industry Sessions > Insight box',

  resources_shared = 'RBI Master Direction on Digital Lending Guidelines (2022, updated 2024)
Bain & Company India FinTech Report 2025 — "Mainstreaming of FinTech"
Inc42 State of Indian FinTech Q1 2026
McKinsey Global Banking Annual Review 2025',

  learning_outcomes = 'Identify the top 3 categories where Indian banks are actually spending FinTech budgets in 2026
Explain why the buyer shifted from CDO to CRO/COO and what that means for FinTech go-to-market
Name 3 FinTechs winning right now and one specific reason each is winning
Articulate why revenue-share has replaced licensing as the dominant partnership model'

WHERE title LIKE '%FinTech in 2026%';

-- ============================================================
-- VERIFY enrichment
-- ============================================================
SELECT id, title,
       JSON_LENGTH(key_topics) AS topics,
       LENGTH(session_outline) AS outline_chars,
       LENGTH(examples_discussed) AS examples_chars,
       LENGTH(case_studies) AS cases_chars,
       LENGTH(key_quotes) AS quotes_chars,
       LENGTH(assignments_given) AS assignments_chars,
       LENGTH(learning_outcomes) AS outcomes_chars
FROM industry_sessions
WHERE title LIKE '%FinTech%';