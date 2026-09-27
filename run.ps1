# Start backend + frontend in separate PowerShell windows.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
$Logs = Join-Path $Root "logs"
New-Item -ItemType Directory -Path $Logs -Force | Out-Null

if (-not (Test-Path ".env") -and (Test-Path ".env.example")) {
  Copy-Item ".env.example" ".env"
  Write-Host "Created .env from .env.example. Fill in your API keys before connecting mailboxes."
}

Write-Host "Starting AI Auto-Email Checker..."
Write-Host "  Backend:  http://127.0.0.1:8000"
Write-Host "  Frontend: http://127.0.0.1:5173"
Write-Host ""

& (Join-Path $Root "scripts\run-postgres.ps1")

Start-Process powershell -ArgumentList @(
  "-NoProfile",
  "-ExecutionPolicy", "Bypass",
  "-File", (Join-Path $Root "scripts\run-backend.ps1")
) -WindowStyle Hidden `
  -RedirectStandardOutput (Join-Path $Logs "backend.log") `
  -RedirectStandardError (Join-Path $Logs "backend-error.log")
Start-Sleep -Seconds 2
Start-Process powershell -ArgumentList @(
  "-NoProfile",
  "-ExecutionPolicy", "Bypass",
  "-File", (Join-Path $Root "scripts\run-frontend.ps1")
) -WindowStyle Hidden `
  -RedirectStandardOutput (Join-Path $Logs "frontend.log") `
  -RedirectStandardError (Join-Path $Logs "frontend-error.log")

$ngrok = Get-Command ngrok -ErrorAction SilentlyContinue
if ($ngrok) {
  $tunnelRunning = $false
  try {
    $null = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:4040/api/tunnels" -TimeoutSec 2
    $tunnelRunning = $true
  } catch {
    $tunnelRunning = $false
  }

  if (-not $tunnelRunning) {
    $webhookBaseUrl = ""
    Get-Content (Join-Path $Root ".env") | ForEach-Object {
      if ($_ -match '^WEBHOOK_BASE_URL\s*=\s*(.+?)\s*$') {
        $webhookBaseUrl = $Matches[1].Trim().Trim([char]34).Trim([char]39)
      }
    }
    $ngrokArgs = @("http", "http://127.0.0.1:8000")
    if ($webhookBaseUrl -match '^https://.+\.ngrok(?:-free)?\.(?:app|dev)$') {
      $ngrokArgs += @("--url", $webhookBaseUrl)
    }
    Start-Process -FilePath $ngrok.Source -ArgumentList $ngrokArgs -WindowStyle Hidden `
      -RedirectStandardOutput (Join-Path $Logs "ngrok.log") `
      -RedirectStandardError (Join-Path $Logs "ngrok-error.log")
    if ($webhookBaseUrl) {
      Write-Host "  Webhook tunnel: $webhookBaseUrl"
    } else {
      Write-Host "  Webhook tunnel: ngrok assigned URL"
    }
  } else {
    Write-Host "  Webhook tunnel: already running"
  }
} else {
  Write-Warning "ngrok was not found. Webhook delivery requires ngrok or another public HTTPS tunnel."
}

Write-Host "Backend, frontend, and webhook tunnel started."
