param([switch]$WithNgrok)

# Start PostgreSQL plus a watchdog that keeps backend and frontend healthy.
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

$Runtime = Join-Path $Root ".tmp"
$SupervisorPidFile = Join-Path $Runtime "service-supervisor.pid"
New-Item -ItemType Directory -Path $Runtime -Force | Out-Null
$supervisorRunning = $false
if (Test-Path $SupervisorPidFile) {
  $supervisorPid = Get-Content $SupervisorPidFile -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($supervisorPid -and (Get-Process -Id $supervisorPid -ErrorAction SilentlyContinue)) {
    $supervisorRunning = $true
  }
}
if (-not $supervisorRunning) {
  Start-Process powershell -ArgumentList @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", (Join-Path $Root "scripts\supervise-services.ps1")
  ) -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $Logs "supervisor-output.log") `
    -RedirectStandardError (Join-Path $Logs "supervisor-error.log")
  Start-Sleep -Seconds 3
} else {
  Write-Host "  Service supervisor: already running"
}

if ($WithNgrok) {
  $ngrok = Get-Command ngrok -ErrorAction SilentlyContinue
  if (-not $ngrok) {
    Write-Warning "ngrok was not found. Webhook delivery will use polling fallback."
  } else {
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
  }
} else {
  Write-Host "  Webhook tunnel: not started (use .\run.ps1 -WithNgrok)"
}

Write-Host "PostgreSQL, backend, frontend, and service supervisor started."
