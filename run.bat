@echo off
setlocal
title AI Auto-Email Checker - start
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" -WithNgrok %*
set "code=%errorlevel%"
echo.
if not "%code%"=="0" (
  echo Start failed with exit code %code%. Check the logs folder for details.
  pause
) else (
  echo The app keeps running in the background. Use stop.bat to shut it down.
  timeout /t 5 >nul 2>&1
)
exit /b %code%
