@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
    python -B -X utf8 run.py demo
) else (
    py -3 -B -X utf8 run.py demo
)
if errorlevel 1 pause
