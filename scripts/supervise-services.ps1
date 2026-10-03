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
$env:PATH = (Join-Path $Root ".local\postgresql\bin") + ";" + $env:PATH

$backendProcess = $null
$frontendProcess = $null
$backendStartedAt = Get-Date
$frontendStartedAt = Get-Date
$backendFailures = 0
$frontendFailures = 0
# Calendar backfill runs before the server accepts connections.
$startupGraceSeconds = 300

function Write-SupervisorLog([string]$Message) {
    Add-Content -LiteralPath (Join-Path $Logs "supervisor.log") `
        -Value "[$(Get-Date -Format s)] $Message"
}

function Test-Endpoint([string]$Url) {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 3
        return $response.StatusCode -ge 200 -and $response.StatusCode -lt 500
    } catch {
        return $false
    }
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
    # 0.0.0.0 lets other computers on the LAN open the app; the Windows
    # firewall rule decides which addresses may actually connect.
    Write-SupervisorLog "Starting frontend on 0.0.0.0:5173"
    return Start-Process -FilePath $Node `
        -ArgumentList @($Vite, "--host", "0.0.0.0", "--port", "5173", "--strictPort") `
        -WorkingDirectory $Frontend -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $Logs "frontend.log") `
        -RedirectStandardError (Join-Path $Logs "frontend-error.log")
}

try {
    while ($true) {
        if (-not $backendProcess -or $backendProcess.HasExited) {
            $backendProcess = Start-Backend
            $backendStartedAt = Get-Date
            $backendFailures = 0
            Start-Sleep -Seconds 3
        }
        if (Test-Endpoint "http://127.0.0.1:8000/api/health") {
            $backendFailures = 0
        } elseif (((Get-Date) - $backendStartedAt).TotalSeconds -ge $startupGraceSeconds) {
            $backendFailures++
            if ($backendFailures -ge 3) {
                Write-SupervisorLog "Backend unhealthy; restarting PID $($backendProcess.Id)"
                Stop-Process -Id $backendProcess.Id -Force -ErrorAction SilentlyContinue
                $backendProcess = $null
                $backendFailures = 0
            }
        }

        if (-not $frontendProcess -or $frontendProcess.HasExited) {
            if (Test-Endpoint "http://127.0.0.1:5173") {
                $frontendFailures = 0
            } else {
                $frontendProcess = Start-Frontend
                $frontendStartedAt = Get-Date
                $frontendFailures = 0
                Start-Sleep -Seconds 2
            }
        }
        if (Test-Endpoint "http://127.0.0.1:5173") {
            $frontendFailures = 0
        } elseif (((Get-Date) - $frontendStartedAt).TotalSeconds -ge $startupGraceSeconds) {
            $frontendFailures++
            if ($frontendFailures -ge 3) {
                Write-SupervisorLog "Frontend unhealthy; restarting PID $($frontendProcess.Id)"
                Stop-Process -Id $frontendProcess.Id -Force -ErrorAction SilentlyContinue
                $frontendProcess = $null
                $frontendFailures = 0
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
