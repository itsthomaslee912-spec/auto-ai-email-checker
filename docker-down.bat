@echo off
setlocal
title AI Auto-Email Checker - Docker stop
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\docker-down.ps1" %*
set "code=%errorlevel%"
echo.
if not "%code%"=="0" (
  echo Docker stop finished with warnings ^(exit code %code%^).
  pause
) else (
  timeout /t 3 >nul 2>&1
)
exit /b %code%
