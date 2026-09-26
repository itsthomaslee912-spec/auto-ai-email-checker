@echo off
setlocal
cd /d "%~dp0.."

echo === Setup: AI Auto-Email Checker ===

if not exist ".env" (
  if exist ".env.example" (
    copy /Y ".env.example" ".env" >nul
    echo Created .env from .env.example
  )
)

echo.
echo [1/2] Backend Python venv...
if not exist "backend\.venv\Scripts\python.exe" (
  python -m venv backend\.venv
)
backend\.venv\Scripts\python.exe -m pip install --upgrade pip
backend\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
if errorlevel 1 exit /b 1

echo.
echo [2/2] Frontend npm packages...
where node >nul 2>&1
if errorlevel 1 (
  echo WARNING: Node.js not found. Install from https://nodejs.org/ then re-run setup.
) else (
  pushd frontend
  call npm install
  popd
)

echo.
echo Setup complete. Run run.bat to start both servers.
endlocal
