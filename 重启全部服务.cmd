@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -X utf8 run_services.py restart
echo.
echo Logs and status: reports\services-status.json
pause
