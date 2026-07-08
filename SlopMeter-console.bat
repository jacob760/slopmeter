@echo off
setlocal
title SlopMeter (console)
cd /d "%~dp0"
where py >nul 2>&1 && (set "RUN=py") || (set "RUN=python")
%RUN% "%~dp0eqdps.py" %*
echo.
echo ============================================
echo  SlopMeter stopped (exit code %errorlevel%)
echo ============================================
pause
