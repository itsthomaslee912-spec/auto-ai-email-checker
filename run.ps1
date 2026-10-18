param([switch]$WithNgrok, [switch]$NoBrowser)

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
Write-Host "  Frontend: http://127.0.0.1:5173 (LAN: http://<this-pc-ip>:5173)"
Write-Host ""

try {
  & (Join-Path $Root "scripts\run-postgres.ps1")
} catch {
  Write-Warning "PostgreSQL did not start yet: $($_.Exception.Message)"
  Write-Warning "The service supervisor will keep retrying it in the background."
}

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

function Test-Endpoint([string]$Url) {
  try {
    $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 3
    return $response.StatusCode -ge 200 -and $response.StatusCode -lt 300
  } catch {
    return $false
  }
}

Write-Host ""
Write-Host "Waiting for backend and frontend to respond..."
$deadline = (Get-Date).AddSeconds(120)
$backendUp = $false
$frontendUp = $false
while ((Get-Date) -lt $deadline -and -not ($backendUp -and $frontendUp)) {
  if (-not $backendUp) { $backendUp = Test-Endpoint "http://127.0.0.1:8000/api/health" }
  if (-not $frontendUp) { $frontendUp = Test-Endpoint "http://127.0.0.1:5173" }
  if (-not ($backendUp -and $frontendUp)) { Start-Sleep -Seconds 2 }
}
Write-Host ("  Backend:  " + $(if ($backendUp) { "ready" } else { "NOT responding (see logs\backend-error.log)" }))
Write-Host ("  Frontend: " + $(if ($frontendUp) { "ready" } else { "NOT responding (see logs\frontend-error.log)" }))
if (-not ($backendUp -and $frontendUp)) {
  Write-Warning "The app did not become ready within 2 minutes. The supervisor keeps retrying in the background; run stop.bat to shut everything down."
  exit 1
}

Write-Host ""
Write-Host "AI Auto-Email Checker is running at http://localhost:5173"
if (-not $NoBrowser) { Start-Process "http://localhost:5173" }
