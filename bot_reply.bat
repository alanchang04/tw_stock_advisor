@echo off
chcp 65001 >nul
cd /d "C:\Users\alanchang\Desktop\taiwan_stock_advisor"
set PYTHONIOENCODING=utf-8
"C:\Users\alanchang\AppData\Local\Programs\Python\Python312\python.exe" run_pipeline.py --mode bot >> logs\bot_reply.log 2>&1
