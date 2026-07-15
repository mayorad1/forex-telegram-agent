@echo off
cd /d C:\Users\HP\forex-telegram-agent
set PYTHONUNBUFFERED=1
".venv\Scripts\python.exe" -u -m src.main >> runtime\bot.out.log 2>> runtime\bot.err.log
