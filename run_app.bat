@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if not exist ".venv\Scripts\python.exe" (
    echo ERROR: .venv not found. Install dependencies first.
    exit /b 1
)
".venv\Scripts\python.exe" -m streamlit run app.py --server.port 8501 --server.headless false
