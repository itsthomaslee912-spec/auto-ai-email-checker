$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Backend = Join-Path $Root "backend"
$Frontend = Join-Path $Root "frontend"
$Logs = Join-Path $Root "logs"
$Runtime = Join-Path $Root ".tmp"
$Python = Join-Path $Backend ".venv\Scripts\python.exe"
$Node = (Get-Command node.exe -ErrorAction Stop).Source
$Vite = Join-Path $Frontend "node_modules\vite\bin\vite.js"

New-Item -ItemType Directory -Force -Path $Logs, $Runtime | Out-Null
Set-Content -LiteralPath (Join-Path $Runtime "service-supervisor.pid") -Value $PID

if (-not (Test-Path $Python)) { throw "Backend virtual environment is missing." }
if (-not (Test-Path $Vite)) { throw "Frontend dependencies are missing." }

$env:DISABLE_SQLALCHEMY_CEXT_RUNTIME = "1"
$env:PSYCOPG_IMPL = "python"
$env:PYTHONUNBUFFERED = "1"
$env:PATH = (Join-Path $Root ".local\postgresql\bin") + ";" + $env:PATH

$backendProcess = $null
$frontendProcess = $null
$backendStartedAt = Get-Date
$frontendStartedAt = Get-Date
$backendFailures = 0
$frontendFailures = 0
$backendRestartDelay = 0
$frontendRestartDelay = 0
# The API answers /api/health before background repair starts, so a hung
# process should be replaced quickly instead of staying down for minutes.
$startupGraceSeconds = 90
# Sync, calendar extraction, and local-model work can temporarily starve the
# HTTP event loop even though useful work is still progressing. A short probe
# outage must not create a restart/retry storm that drops provider traffic.
$backendFailureThreshold = 30

function Write-SupervisorLog([string]$Message) {
    Add-Content -LiteralPath (Join-Path $Logs "supervisor.log") `
        -Value "[$(Get-Date -Format s)] $Message"
}

function Test-Endpoint([string]$Url) {
    try {
        # Reclassify / sync can make the API briefly busy; don't false-restart
        # a healthy process just because one probe took longer than 3s.
        $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 10
        return $response.StatusCode -ge 200 -and $response.StatusCode -lt 300
    } catch {
        return $false
    }
}

function Ensure-Postgres {
    try {
        $pgCtl = Join-Path $Root ".local\postgresql\bin\pg_ctl.exe"
        $data = Join-Path $Root ".local\postgresql\data"
        if (-not (Test-Path $pgCtl) -or -not (Test-Path (Join-Path $data "PG_VERSION"))) { return }
        & $pgCtl -D $data status *> $null
        if ($LASTEXITCODE -eq 0) { return }
        Write-SupervisorLog "PostgreSQL is down; starting it"
        & (Join-Path $Root "scripts\run-postgres.ps1")
    } catch {
        Write-SupervisorLog "PostgreSQL could not be started: $($_.Exception.Message)"
    }
}

function Get-RestartDelay([int]$Current, [datetime]$StartedAt) {
    $ran = ((Get-Date) - $StartedAt).TotalSeconds
    if ($ran -ge 30) { return 0 }
    if ($Current -lt 3) { return 3 }
    return [Math]::Min(30, $Current * 2)
}

# A process we force-kill for being unhealthy has, by construction, already
# run past $startupGraceSeconds (90s) by the time $backendFailures/
# $frontendFailures reaches its threshold. Get-RestartDelay's own "ran >= 30s
# means it was fine, reset to 0" rule fires unconditionally in that case, so
# calling it here always discarded the backoff and let a hung-but-never-
# exiting process get force-killed and immediately respawned forever. This
# grows the delay directly instead, with no uptime check.
function Get-GrownDelay([int]$Current) {
    if ($Current -lt 3) { return 3 }
    return [Math]::Min(30, $Current * 2)
}

function Start-Backend {
    Write-SupervisorLog "Starting backend on 127.0.0.1:8000"
    return Start-Process -FilePath $Python `
        -ArgumentList @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000") `
        -WorkingDirectory $Backend -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $Logs "backend.log") `
        -RedirectStandardError (Join-Path $Logs "backend-error.log")
}

function Start-Frontend {
    # "::" is dual-stack: other computers on the LAN can open the app, and
    # browsers resolving localhost to ::1 connect without a ~300 ms IPv4
    # fallback per request. The Windows firewall rule decides which
    # addresses may actually connect.
    Write-SupervisorLog "Starting frontend on [::]:5173"
    return Start-Process -FilePath $Node `
        -ArgumentList @($Vite, "--host", "::", "--port", "5173", "--strictPort") `
        -WorkingDirectory $Frontend -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $Logs "frontend.log") `
        -RedirectStandardError (Join-Path $Logs "frontend-error.log")
}

try {
    while ($true) {
        Ensure-Postgres
        if ($backendProcess -and $backendProcess.HasExited) {
            $backendRestartDelay = Get-RestartDelay $backendRestartDelay $backendStartedAt
            if ($backendRestartDelay -gt 0) {
                Write-SupervisorLog "Backend exited; waiting ${backendRestartDelay}s before starting it again"
                Start-Sleep -Seconds $backendRestartDelay
            }
        }
        if (-not $backendProcess -or $backendProcess.HasExited) {
            $backendProcess = Start-Backend
            $backendStartedAt = Get-Date
            $backendFailures = 0
            Start-Sleep -Seconds 3
        }
        if (Test-Endpoint "http://127.0.0.1:8000/api/health") {
            $backendFailures = 0
            if (((Get-Date) - $backendStartedAt).TotalSeconds -ge 30) { $backendRestartDelay = 0 }
        } elseif (((Get-Date) - $backendStartedAt).TotalSeconds -ge $startupGraceSeconds) {
            $backendFailures++
            if ($backendFailures -ge $backendFailureThreshold) {
                Write-SupervisorLog "Backend unhealthy; restarting PID $($backendProcess.Id)"
                Stop-Process -Id $backendProcess.Id -Force -ErrorAction SilentlyContinue
                $backendProcess = $null
                $backendFailures = 0
                $backendRestartDelay = Get-GrownDelay $backendRestartDelay
            }
        }

        if ($frontendProcess -and $frontendProcess.HasExited) {
            $frontendRestartDelay = Get-RestartDelay $frontendRestartDelay $frontendStartedAt
            if ($frontendRestartDelay -gt 0) {
                Write-SupervisorLog "Frontend exited; waiting ${frontendRestartDelay}s before starting it again"
                Start-Sleep -Seconds $frontendRestartDelay
            }
        }
        if (-not $frontendProcess -or $frontendProcess.HasExited) {
            if (Test-Endpoint "http://127.0.0.1:5173") {
                $frontendFailures = 0
                $frontendRestartDelay = 0
            } else {
                $frontendProcess = Start-Frontend
                $frontendStartedAt = Get-Date
                $frontendFailures = 0
                Start-Sleep -Seconds 2
            }
        }
        if (Test-Endpoint "http://127.0.0.1:5173") {
            $frontendFailures = 0
            if (((Get-Date) - $frontendStartedAt).TotalSeconds -ge 30) { $frontendRestartDelay = 0 }
        } elseif ($frontendProcess -and ((Get-Date) - $frontendStartedAt).TotalSeconds -ge $startupGraceSeconds) {
            $frontendFailures++
            if ($frontendFailures -ge 3) {
                Write-SupervisorLog "Frontend unhealthy; restarting PID $($frontendProcess.Id)"
                Stop-Process -Id $frontendProcess.Id -Force -ErrorAction SilentlyContinue
                $frontendProcess = $null
                $frontendFailures = 0
                $frontendRestartDelay = Get-GrownDelay $frontendRestartDelay
            }
        }
        Start-Sleep -Seconds 5
    }
} finally {
    foreach ($process in @($backendProcess, $frontendProcess)) {
        if ($process -and -not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        }
    }
    Remove-Item -LiteralPath (Join-Path $Runtime "service-supervisor.pid") -Force -ErrorAction SilentlyContinue
}
