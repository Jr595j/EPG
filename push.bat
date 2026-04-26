@echo off
title Push to GitHub
cd /d "%~dp0"

echo ============================================================
echo  Pushing changes to GitHub...
echo ============================================================
echo.

git add -u
git status --short
echo.

set /p MSG="Commit message (or press Enter for default): "
if "%MSG%"=="" set MSG=Update %date% %time:~0,5%

git commit -m "%MSG%"
git push

echo.
echo Done!
echo ============================================================
pause
