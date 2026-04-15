@echo off
title EPG Aggregator
cd /d "%~dp0"

echo ============================================================
echo  EPG Aggregator
echo ============================================================

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Install from https://python.org
    pause
    exit /b 1
)

:: Install dependencies if needed
echo Installing/checking dependencies...
pip install -r requirements.txt --quiet

echo.
echo Starting server (Starlite)...
echo  Dashboard : http://localhost:8080/
echo  EPG URL   : http://localhost:8080/epg.xml
echo.
echo To also run the MyBunny instance, open another terminal and run:
echo   start_mybunny.bat   (port 8081)
echo.
echo Press Ctrl+C to stop.
echo ============================================================
echo.

python server.py --config config.json

pause
