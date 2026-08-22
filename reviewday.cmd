@echo off
REM One command reviews one assignment end-to-end and builds BOTH lists:
REM   results_report_ID.csv   every student: name, email, marks, feedback, reason
REM   problem_report_ID.csv   only the students who must act, with probe check
REM Usage:   reviewday 19     (the id number from the admin page URL)
cd /d "%~dp0"
if "%~1"=="" (
  echo Usage: reviewday ASSIGNMENT_ID
  echo Find the id number in the admin page URL for that assignment.
  exit /b 1
)
python tools\bulk_review.py --assignment-id %1 --run --limit 500 --concurrency 2
python tools\problem_report.py --assignment-id %1 --probe
copy /y problem_report.csv problem_report_%1.csv >nul 2>&1
python tools\results_report.py --assignment-id %1
echo.
echo Done for assignment %1.
echo   Full class roster : results_report_%1.csv
echo   Students to message: problem_report_%1.csv
pause
