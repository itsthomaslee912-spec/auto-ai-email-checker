@echo off
setlocal
cd /d "%~dp0..\frontend"

where node >nul 2>&1
if errorlevel 1 (
  echo Node.js is required for the frontend.
  echo Install from https://nodejs.org/ then run this again.
  exit /b 1
)

if not exist "node_modules" (
  echo Installing frontend dependencies...
  call npm install
  if errorlevel 1 exit /b 1
)

echo Frontend running at http://127.0.0.1:5173
call npm run dev
endlocal
