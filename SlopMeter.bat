@echo off
setlocal
title SlopMeter
cd /d "%~dp0"
rem overlay UI - no console window (pythonw). Falls back through pyw -> pythonw -> py.
where pyw >nul 2>&1 && (set "RUN=pyw") || (where pythonw >nul 2>&1 && (set "RUN=pythonw") || (set "RUN=py"))
start "" %RUN% "%~dp0eqdps_ui.py" %*
