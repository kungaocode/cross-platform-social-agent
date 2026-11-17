@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PROJECT_PYTHON=python"
if exist "%~dp0.venv\Scripts\python.exe" set "PROJECT_PYTHON=%~dp0.venv\Scripts\python.exe"
echo Starting WeChat, QQ, Bilibili and Douyin API chat with browser comments.
"%PROJECT_PYTHON%" -X utf8 run_services.py start
echo.
echo Logs and status: reports\services-status.json
pause
