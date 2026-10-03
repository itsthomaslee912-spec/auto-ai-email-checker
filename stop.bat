@echo off
setlocal
title AI Auto-Email Checker - stop
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop-servers.ps1"
set "code=%errorlevel%"
echo.
if not "%code%"=="0" (
  echo Stop finished with warnings ^(exit code %code%^).
  pause
) else (
  timeout /t 3 >nul 2>&1
)
exit /b %code%
