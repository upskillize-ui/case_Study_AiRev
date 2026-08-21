@echo off
REM Final targeted sweep for assignment 17: re-scores every ungraded row and
REM every row below 7/10 under the final rules; rows at/above 7 keep their
REM mark and cost nothing. Then builds the student problem list with probe.
cd /d "%~dp0"
for /L %%i in (0,25,450) do python tools\bulk_review.py --assignment-id 17 --limit 25 --offset %%i --run --redo --redo-below 7 --concurrency 2
python tools\problem_report.py --assignment-id 17 --probe
echo.
echo Sweep finished. Student list: problem_report.csv
pause
