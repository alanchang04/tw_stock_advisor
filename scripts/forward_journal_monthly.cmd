@echo off
REM Monthly forward journal runner (registered in Windows Task Scheduler).
REM
REM Deliberately dumb: no arguments, no options. It runs the frozen journal and
REM appends a line to a log. If the definitions ever drift, the Python script
REM refuses to write and this log records the refusal.
REM
REM Remove the schedule with:  schtasks /delete /tn "TWStockForwardJournal" /f

setlocal
REM Log is UTF-8; without this the Chinese output is mangled by the console codepage.
set PYTHONIOENCODING=utf-8
set REPO=%~dp0..
set LOG=%REPO%\reports\forward_journal_run.log

cd /d "%REPO%" || exit /b 1

echo. >> "%LOG%"
echo ==== %DATE% %TIME% ==== >> "%LOG%"

if exist "%REPO%\.venv\Scripts\python.exe" (
    "%REPO%\.venv\Scripts\python.exe" scripts\run_forward_journal.py >> "%LOG%" 2>&1
) else (
    python scripts\run_forward_journal.py >> "%LOG%" 2>&1
)

echo exit code: %ERRORLEVEL% >> "%LOG%"
endlocal
