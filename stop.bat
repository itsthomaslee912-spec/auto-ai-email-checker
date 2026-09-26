@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop-servers.ps1"
exit /b %errorlevel%
