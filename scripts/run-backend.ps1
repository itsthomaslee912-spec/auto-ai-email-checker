$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$Python = Join-Path $Root "backend\.venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
  Write-Host "Creating Python venv and installing dependencies..."
  python -m venv (Join-Path $Root "backend\.venv")
  & $Python -m pip install --upgrade pip
  & $Python -m pip install -r (Join-Path $Root "backend\requirements.txt")
}

Set-Location (Join-Path $Root "backend")
Write-Host "Backend running at http://127.0.0.1:8000"
$env:DISABLE_SQLALCHEMY_CEXT_RUNTIME = "1"
$env:PSYCOPG_IMPL = "python"
$env:PATH = (Join-Path $Root ".local\postgresql\bin") + ";" + $env:PATH
& $Python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
