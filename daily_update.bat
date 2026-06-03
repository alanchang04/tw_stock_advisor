@echo off
REM ============================================================
REM  台股顧問系統 — 每日自動更新
REM  流程：啟動 Docker → 等資料庫就緒 → 抓資料 + 技術指標 + 推薦
REM  給 Windows 工作排程器呼叫；輸出寫入 logs\daily_update.log
REM ============================================================
cd /d "C:\Users\alanchang\Desktop\taiwan_stock_advisor"

echo [%date% %time%] === 每日更新開始 === >> logs\daily_update.log

REM 1. 啟動資料庫（Docker Desktop 需在執行中）
docker compose up -d >> logs\daily_update.log 2>&1

REM 2. 等待 Postgres 就緒（最多約 60 秒）
set /a tries=0
:waitdb
docker exec stock_advisor_db pg_isready -U stock_user -d taiwan_stock >nul 2>&1
if %errorlevel%==0 goto dbready
set /a tries+=1
if %tries% geq 30 goto dbfail
timeout /t 2 /nobreak >nul
goto waitdb

:dbready
echo [%date% %time%] 資料庫就緒，執行每日流程 >> logs\daily_update.log
py -3.12 run_pipeline.py --mode pipeline >> logs\daily_update.log 2>&1
echo [%date% %time%] === 每日更新完成 === >> logs\daily_update.log
exit /b 0

:dbfail
echo [%date% %time%] [錯誤] 資料庫啟動逾時，請確認 Docker Desktop 是否在執行 >> logs\daily_update.log
exit /b 1
