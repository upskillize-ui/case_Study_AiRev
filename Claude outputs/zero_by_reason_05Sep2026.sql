-- AiRev / LMS course 55 — mark every sent-back row 0 with its student-side reason (05 Sep 2026)
-- Pure SQL: no reader, no sweep, no AI call. Run the SELECT at the end first if you want a preview.
-- Rows carrying 'could not finish reviewing this attempt' (4824, 10080) are NOT touched — see chat.

-- A. links (private / dead / nothing to read) -> link only
UPDATE assignment_submissions s JOIN assignments a ON a.id = s.assignment_id
   SET s.grade = 0, s.status = 'graded', s.feedback = JSON_OBJECT('zeroed', TRUE, 'reviewedBy', 'mentor', 'blocker', 'link_only', 'grade', 'Link only', 'totalScore', 0, 'scoreMarks', 0, 'outOf', 10,
  'message', 'What reached us is a link only — the work itself was not submitted.\n1. Submit the output itself: a screenshot, PDF, or the file of what you built, together with the link.\n2. If the tool only gives a link, also write a few lines saying what you made and how.\nMarks for now: 0 out of 10. Once you have fixed this, message the Upskillize team to reopen the assignment, then submit again.', 'summary', 'What reached us is a link only — the work itself was not submitted.', 'detailedFeedback', 'What reached us is a link only — the work itself was not submitted.\n1. Submit the output itself: a screenshot, PDF, or the file of what you built, together with the link.\n2. If the tool only gives a link, also write a few lines saying what you made and how.\nMarks for now: 0 out of 10. Once you have fixed this, message the Upskillize team to reopen the assignment, then submit again.',
  'feedbackPoints', JSON_ARRAY('Submit the output itself: a screenshot, PDF, or the file of what you built, together with the link.', 'If the tool only gives a link, also write a few lines saying what you made and how.', 'Marks for now: 0 out of 10. Once you have fixed this, message the Upskillize team to reopen the assignment, then submit again.'),
  'strengths', JSON_ARRAY(), 'improvements', JSON_ARRAY('Submit the output itself: a screenshot, PDF, or the file of what you built, together with the link.', 'If the tool only gives a link, also write a few lines saying what you made and how.'),
  'zeroedAt', DATE_FORMAT(UTC_TIMESTAMP(), '%Y-%m-%dT%H:%i:%s.000Z'))
 WHERE a.course_id = 55 AND s.grade IS NULL AND s.feedback LIKE '%"returned":true%'
   AND (s.feedback LIKE '%"blocker":"link_private"%' OR s.feedback LIKE '%"blocker":"link_dead"%'
     OR s.feedback LIKE '%"blocker":"link_no_text"%');

-- B. wrong task
UPDATE assignment_submissions s JOIN assignments a ON a.id = s.assignment_id
   SET s.grade = 0, s.status = 'graded', s.feedback = JSON_OBJECT('zeroed', TRUE, 'reviewedBy', 'mentor', 'blocker', 'wrong_task', 'grade', 'Not this task', 'totalScore', 0, 'scoreMarks', 0, 'outOf', 10,
  'message', 'The work received is for a different assignment.\n1. Submit the work this assignment asks for.\nMarks for now: 0 out of 10. Once you have fixed this, message the Upskillize team to reopen the assignment, then submit again.', 'summary', 'The work received is for a different assignment.', 'detailedFeedback', 'The work received is for a different assignment.\n1. Submit the work this assignment asks for.\nMarks for now: 0 out of 10. Once you have fixed this, message the Upskillize team to reopen the assignment, then submit again.',
  'feedbackPoints', JSON_ARRAY('Submit the work this assignment asks for.', 'Marks for now: 0 out of 10. Once you have fixed this, message the Upskillize team to reopen the assignment, then submit again.'),
  'strengths', JSON_ARRAY(), 'improvements', JSON_ARRAY('Submit the work this assignment asks for.'),
  'zeroedAt', DATE_FORMAT(UTC_TIMESTAMP(), '%Y-%m-%dT%H:%i:%s.000Z'))
 WHERE a.course_id = 55 AND s.grade IS NULL AND s.feedback LIKE '%"returned":true%'
   AND s.feedback LIKE '%"blocker":"wrong_task"%';

-- C. nothing submitted
UPDATE assignment_submissions s JOIN assignments a ON a.id = s.assignment_id
   SET s.grade = 0, s.status = 'graded', s.feedback = JSON_OBJECT('zeroed', TRUE, 'reviewedBy', 'mentor', 'blocker', 'nothing_submitted', 'grade', 'Nothing received', 'totalScore', 0, 'scoreMarks', 0, 'outOf', 10,
  'message', 'Nothing reached us — no file and no written answer.\n1. Attach your file, or type your answer in the box.\n2. Open the file on your own device first to check it works.\nMarks for now: 0 out of 10. Once you have fixed this, message the Upskillize team to reopen the assignment, then submit again.', 'summary', 'Nothing reached us — no file and no written answer.', 'detailedFeedback', 'Nothing reached us — no file and no written answer.\n1. Attach your file, or type your answer in the box.\n2. Open the file on your own device first to check it works.\nMarks for now: 0 out of 10. Once you have fixed this, message the Upskillize team to reopen the assignment, then submit again.',
  'feedbackPoints', JSON_ARRAY('Attach your file, or type your answer in the box.', 'Open the file on your own device first to check it works.', 'Marks for now: 0 out of 10. Once you have fixed this, message the Upskillize team to reopen the assignment, then submit again.'),
  'strengths', JSON_ARRAY(), 'improvements', JSON_ARRAY('Attach your file, or type your answer in the box.', 'Open the file on your own device first to check it works.'),
  'zeroedAt', DATE_FORMAT(UTC_TIMESTAMP(), '%Y-%m-%dT%H:%i:%s.000Z'))
 WHERE a.course_id = 55 AND s.grade IS NULL AND s.feedback LIKE '%"returned":true%'
   AND s.feedback LIKE '%"blocker":"nothing_submitted"%';

-- D. file with no readable content
UPDATE assignment_submissions s JOIN assignments a ON a.id = s.assignment_id
   SET s.grade = 0, s.status = 'graded', s.feedback = JSON_OBJECT('zeroed', TRUE, 'reviewedBy', 'mentor', 'blocker', 'file_unreadable', 'grade', 'Not readable', 'totalScore', 0, 'scoreMarks', 0, 'outOf', 10,
  'message', 'The file that reached us has no readable content.\n1. If it is a photo or scan, make sure the text is sharp and the right way up.\n2. Or type your answer in the box as well.\nMarks for now: 0 out of 10. Once you have fixed this, message the Upskillize team to reopen the assignment, then submit again.', 'summary', 'The file that reached us has no readable content.', 'detailedFeedback', 'The file that reached us has no readable content.\n1. If it is a photo or scan, make sure the text is sharp and the right way up.\n2. Or type your answer in the box as well.\nMarks for now: 0 out of 10. Once you have fixed this, message the Upskillize team to reopen the assignment, then submit again.',
  'feedbackPoints', JSON_ARRAY('If it is a photo or scan, make sure the text is sharp and the right way up.', 'Or type your answer in the box as well.', 'Marks for now: 0 out of 10. Once you have fixed this, message the Upskillize team to reopen the assignment, then submit again.'),
  'strengths', JSON_ARRAY(), 'improvements', JSON_ARRAY('If it is a photo or scan, make sure the text is sharp and the right way up.', 'Or type your answer in the box as well.'),
  'zeroedAt', DATE_FORMAT(UTC_TIMESTAMP(), '%Y-%m-%dT%H:%i:%s.000Z'))
 WHERE a.course_id = 55 AND s.grade IS NULL AND s.feedback LIKE '%"returned":true%'
   AND s.feedback LIKE '%"blocker":"file_unreadable"%';


-- E. rows the Space itself stamped (link could not be opened), link-only submissions -> link only
UPDATE assignment_submissions s JOIN assignments a ON a.id = s.assignment_id
   SET s.grade = 0, s.status = 'graded', s.feedback = JSON_OBJECT('zeroed', TRUE, 'reviewedBy', 'mentor', 'blocker', 'link_only', 'grade', 'Link only', 'totalScore', 0, 'scoreMarks', 0, 'outOf', 10,
  'message', 'What reached us is a link only — the work itself was not submitted.\n1. Submit the output itself: a screenshot, PDF, or the file of what you built, together with the link.\n2. If the tool only gives a link, also write a few lines saying what you made and how.\nMarks for now: 0 out of 10. Once you have fixed this, message the Upskillize team to reopen the assignment, then submit again.', 'summary', 'What reached us is a link only — the work itself was not submitted.', 'detailedFeedback', 'What reached us is a link only — the work itself was not submitted.\n1. Submit the output itself: a screenshot, PDF, or the file of what you built, together with the link.\n2. If the tool only gives a link, also write a few lines saying what you made and how.\nMarks for now: 0 out of 10. Once you have fixed this, message the Upskillize team to reopen the assignment, then submit again.',
  'feedbackPoints', JSON_ARRAY('Submit the output itself: a screenshot, PDF, or the file of what you built, together with the link.', 'If the tool only gives a link, also write a few lines saying what you made and how.', 'Marks for now: 0 out of 10. Once you have fixed this, message the Upskillize team to reopen the assignment, then submit again.'),
  'strengths', JSON_ARRAY(), 'improvements', JSON_ARRAY('Submit the output itself: a screenshot, PDF, or the file of what you built, together with the link.', 'If the tool only gives a link, also write a few lines saying what you made and how.'),
  'zeroedAt', DATE_FORMAT(UTC_TIMESTAMP(), '%Y-%m-%dT%H:%i:%s.000Z'))
 WHERE a.course_id = 55 AND s.grade IS NULL AND COALESCE(s.status, '') <> 'draft'
   AND s.feedback LIKE '%notGraded%' AND s.feedback NOT LIKE '%"returned":true%'
   AND (s.feedback LIKE '%blocked our automatic reader%' OR s.feedback LIKE '%human-check%'
     OR s.feedback LIKE '%cloudflare%' OR s.feedback LIKE '%own page rather than your work%'
     OR s.feedback LIKE '%asks whoever visits it to sign in%' OR s.feedback LIKE '%no longer opens%'
     OR s.feedback LIKE '%did not open%' OR s.feedback LIKE '%would not open%')
   AND COALESCE(s.file_path, '') NOT LIKE '%res.cloudinary.com%';

-- Preview / after-check: what is still ungraded on course 55, by bucket
SELECT a.title, s.id, s.status, LEFT(s.feedback, 90) AS fb
  FROM assignment_submissions s JOIN assignments a ON a.id = s.assignment_id
 WHERE a.course_id = 55 AND s.grade IS NULL AND COALESCE(s.status, '') <> 'draft'
 ORDER BY a.id, s.id;
