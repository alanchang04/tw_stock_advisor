@echo off
chcp 65001 >nul
cd /d "C:\Users\alanchang\Desktop\taiwan_stock_advisor"
echo [%date% %time%] start >> logs\daily_update.log
start /wait "" docker compose up -d >> logs\daily_update.log 2>&1
set /a tries=0
:waitdb
docker exec stock_advisor_db pg_isready -U stock_user -d taiwan_stock >nul 2>&1
if %errorlevel%==0 goto dbready
set /a tries+=1
if %tries% geq 30 goto dbfail
timeout /t 2 /nobreak >nul
goto waitdb
:dbready
echo [%date% %time%] db ready, run pipeline (mode auto) >> logs\daily_update.log
set PYTHONIOENCODING=utf-8
"C:\Users\alanchang\AppData\Local\Programs\Python\Python312\python.exe" run_pipeline.py --mode auto >> logs\daily_update.log 2>&1
echo [%date% %time%] done (exit %errorlevel%) >> logs\daily_update.log
exit /b 0
:dbfail
echo [%date% %time%] ERROR db timeout, is Docker Desktop running? >> logs\daily_update.log
exit /b 1
