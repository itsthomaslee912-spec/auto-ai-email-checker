@echo off
setlocal
title AI Auto-Email Checker - Docker start
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\docker-up.ps1" -Build %*
set "code=%errorlevel%"
echo.
if not "%code%"=="0" (
  echo Docker start failed with exit code %code%.
  pause
) else (
  timeout /t 4 >nul 2>&1
)
exit /b %code%
