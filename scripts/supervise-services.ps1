param([switch]$Reload)

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
$env:DISTRIBUTED_RUNTIME = "true"
$env:PATH = (Join-Path $Root ".local\postgresql\bin") + ";" + $env:PATH

$backendProcess = $null
$syncWorkerProcess = $null
$classificationWorkerProcess = $null
$eventRelayProcess = $null
$notificationServiceProcess = $null
$frontendProcess = $null
$backendStartedAt = Get-Date
$frontendStartedAt = Get-Date
$backendFailures = 0
$frontendFailures = 0
$backendRestartDelay = 0
$frontendRestartDelay = 0
$startupGraceSeconds = 90
$backendFailureThreshold = 30
$backendSourceVersion = ""
$workerHeartbeatMaxAgeSeconds = 60
$workerHeartbeatStartupGraceSeconds = 90

function Write-SupervisorLog([string]$Message) {
    Add-Content -LiteralPath (Join-Path $Logs "supervisor.log") `
        -Value "[$(Get-Date -Format s)] $Message"
}

function Test-Endpoint([string]$Url) {
    try {
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

function Get-GrownDelay([int]$Current) {
    if ($Current -lt 3) { return 3 }
    return [Math]::Min(30, $Current * 2)
}

function Get-BackendSourceVersion {
    $files = @(Get-ChildItem (Join-Path $Backend "app") -Recurse -File -Filter "*.py")
    if (-not $files.Count) { return "0:0" }
    $latest = ($files | Measure-Object LastWriteTimeUtc -Maximum).Maximum
    return "$($files.Count):$($latest.Ticks)"
}

function Start-Backend {
    Write-SupervisorLog "Starting backend on 127.0.0.1:8000"
    $arguments = @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000")
    if ($Reload) {
        $arguments += @("--reload", "--reload-dir", (Join-Path $Backend "app"))
    }
    return Start-Process -FilePath $Python `
        -ArgumentList $arguments `
        -WorkingDirectory $Backend -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $Logs "backend.log") `
        -RedirectStandardError (Join-Path $Logs "backend-error.log")
}

function Start-BackendWorker([string]$Name, [string]$Module) {
    Write-SupervisorLog "Starting $Name"
    return Start-Process -FilePath $Python `
        -ArgumentList @("-m", $Module) `
        -WorkingDirectory $Backend -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $Logs "$Name.log") `
        -RedirectStandardError (Join-Path $Logs "$Name-error.log")
}

function Get-WorkerHeartbeatPath([string]$Name) {
    return Join-Path ([System.IO.Path]::GetTempPath()) "$Name-heartbeat"
}

function Test-WorkerHeartbeat($Process, [string]$Name) {
    if (-not $Process -or $Process.HasExited) { return $false }
    $runningSeconds = ((Get-Date) - $Process.StartTime).TotalSeconds
    $heartbeatPath = Get-WorkerHeartbeatPath $Name
    if (-not (Test-Path -LiteralPath $heartbeatPath)) {
        return $runningSeconds -lt $workerHeartbeatStartupGraceSeconds
    }
    $heartbeat = Get-Item -LiteralPath $heartbeatPath -ErrorAction SilentlyContinue
    if (-not $heartbeat) {
        return $runningSeconds -lt $workerHeartbeatStartupGraceSeconds
    }
    # An old heartbeat file must not make a newly launched worker look ready.
    if ($heartbeat.LastWriteTimeUtc -lt $Process.StartTime.ToUniversalTime()) {
        return $runningSeconds -lt $workerHeartbeatStartupGraceSeconds
    }
    return (([datetime]::UtcNow - $heartbeat.LastWriteTimeUtc).TotalSeconds -le $workerHeartbeatMaxAgeSeconds)
}

function Stop-ServiceProcessTree($Process) {
    if (-not $Process -or $Process.HasExited) { return }
    # The virtualenv launcher creates a child python.exe. Stop-Process only
    # kills the launcher and can leave the real worker orphaned, so terminate
    # this narrowly scoped worker tree as one unit.
    & taskkill.exe /PID $Process.Id /T /F *> $null
}

function Ensure-BackendWorker($Process, [string]$Name, [string]$Module) {
    if ($Process -and -not $Process.HasExited -and -not (Test-WorkerHeartbeat $Process $Name)) {
        $heartbeatPath = Get-WorkerHeartbeatPath $Name
        $lastSeen = if (Test-Path -LiteralPath $heartbeatPath) {
            (Get-Item -LiteralPath $heartbeatPath).LastWriteTime.ToString("s")
        } else {
            "never"
        }
        Write-SupervisorLog "$Name heartbeat is stale (last seen $lastSeen); restarting process tree"
        Stop-ServiceProcessTree $Process
        $Process = $null
    }
    if (-not $Process -or $Process.HasExited) {
        return Start-BackendWorker $Name $Module
    }
    return $Process
}

function Start-Frontend {
    Write-SupervisorLog "Starting frontend on [::]:5173"
    return Start-Process -FilePath $Node `
        -ArgumentList @($Vite, "--host", "::", "--port", "5173", "--strictPort") `
        -WorkingDirectory $Frontend -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $Logs "frontend.log") `
        -RedirectStandardError (Join-Path $Logs "frontend-error.log")
}

if ($Reload) {
    $backendSourceVersion = Get-BackendSourceVersion
    Write-SupervisorLog "Backend automatic reload is enabled"
}

try {
    while ($true) {
        Ensure-Postgres
        if ($Reload) {
            $currentSourceVersion = Get-BackendSourceVersion
            if ($currentSourceVersion -ne $backendSourceVersion) {
                $backendSourceVersion = $currentSourceVersion
                Write-SupervisorLog "Backend source changed; restarting worker services"
                foreach ($worker in @(
                    $syncWorkerProcess,
                    $classificationWorkerProcess,
                    $eventRelayProcess,
                    $notificationServiceProcess
                )) {
                    if ($worker -and -not $worker.HasExited) {
                        Stop-ServiceProcessTree $worker
                    }
                }
                $syncWorkerProcess = $null
                $classificationWorkerProcess = $null
                $eventRelayProcess = $null
                $notificationServiceProcess = $null
            }
        }
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

        $syncWorkerProcess = Ensure-BackendWorker $syncWorkerProcess "sync-worker" "app.workers.sync"
        $classificationWorkerProcess = Ensure-BackendWorker $classificationWorkerProcess "classification-worker" "app.workers.classification"
        $eventRelayProcess = Ensure-BackendWorker $eventRelayProcess "event-relay" "app.workers.event_relay"
        $notificationServiceProcess = Ensure-BackendWorker $notificationServiceProcess "notification-service" "app.workers.notification"

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
    foreach ($process in @(
        $backendProcess,
        $syncWorkerProcess,
        $classificationWorkerProcess,
        $eventRelayProcess,
        $notificationServiceProcess,
        $frontendProcess
    )) {
        if ($process -and -not $process.HasExited) {
            Stop-ServiceProcessTree $process
        }
    }
    Remove-Item -LiteralPath (Join-Path $Runtime "service-supervisor.pid") -Force -ErrorAction SilentlyContinue
}
