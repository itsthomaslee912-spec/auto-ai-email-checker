@echo off
setlocal
cd /d "%~dp0.."

if not exist "backend\.venv\Scripts\python.exe" (
  echo Creating Python venv and installing dependencies...
  python -m venv backend\.venv
  if errorlevel 1 (
    echo Python is required. Install Python 3.11+ and retry.
    exit /b 1
  )
  backend\.venv\Scripts\python.exe -m pip install --upgrade pip
  backend\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
  if errorlevel 1 exit /b 1
)

cd backend
echo Backend running at http://127.0.0.1:8000
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
endlocal
