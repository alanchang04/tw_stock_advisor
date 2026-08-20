@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if not exist ".venv\Scripts\python.exe" (
    echo ERROR: .venv not found. Install dependencies first.
    exit /b 1
)
".venv\Scripts\python.exe" run_pipeline.py --mode bot >> logs\bot_reply.log 2>&1
