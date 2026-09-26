$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Frontend = Join-Path $Root "frontend"
Set-Location $Frontend

if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
  Write-Error "Node.js is required for the frontend. Install from https://nodejs.org/ then retry."
}

if (-not (Test-Path "node_modules")) {
  Write-Host "Installing frontend dependencies..."
  npm install
}

Write-Host "Frontend running at http://127.0.0.1:5173"
npm run dev
