$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path.TrimEnd("\")
$escapedRoot = [regex]::Escape($root)
$pgCtl = Join-Path $root ".local\postgresql\bin\pg_ctl.exe"
$pgData = Join-Path $root ".local\postgresql\data"
$supervisorPidFile = Join-Path $root ".tmp\service-supervisor.pid"

function Stop-ProcessTree([object[]]$Roots, [object[]]$Processes) {
    if (-not $Roots.Count) { return 0 }
    $depth = @{}
    foreach ($process in $Roots) { $depth[[int]$process.ProcessId] = 0 }
    do {
        $added = $false
        foreach ($process in $Processes) {
            $processId = [int]$process.ProcessId
            $parentId = [int]$process.ParentProcessId
            if (-not $depth.ContainsKey($processId) -and $depth.ContainsKey($parentId)) {
                $depth[$processId] = $depth[$parentId] + 1
                $added = $true
            }
        }
    } while ($added)
    $targets = @($Processes | Where-Object { $depth.ContainsKey([int]$_.ProcessId) } |
        Sort-Object { $depth[[int]$_.ProcessId] } -Descending)
    foreach ($process in $targets) {
        Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
    }
    return $targets.Count
}

function Test-PortListening([int]$Port) {
    [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

Write-Host "Stopping AI Auto-Email Checker..."

# The supervisor restarts anything that dies (PostgreSQL included), so it must go first.
$processes = @(Get-CimInstance Win32_Process)
$supervisors = @($processes | Where-Object {
    $_.CommandLine -and $_.CommandLine -match "$escapedRoot\\scripts\\supervise-services\.ps1"
})
$stopped = Stop-ProcessTree $supervisors $processes
Remove-Item -LiteralPath $supervisorPidFile -Force -ErrorAction SilentlyContinue
Write-Host ("  Service supervisor: " + $(if ($supervisors.Count) { "stopped" } else { "not running" }))

$processes = @(Get-CimInstance Win32_Process)
$services = @($processes | Where-Object {
    $line = $_.CommandLine
    if (-not $line) { return $false }
    ($line -match "$escapedRoot\\scripts\\run-(backend|frontend)\.bat") -or
    ($line -match "$escapedRoot\\backend\\\.venv\\Scripts\\python\.exe" -and $line -match "uvicorn app\.main:app") -or
    ($line -match "$escapedRoot\\backend\\\.venv\\Scripts\\python\.exe" -and $line -match "app\.workers\.(sync|classification|event_relay|notification)") -or
    ($line -match "$escapedRoot\\frontend\\node_modules\\.*vite\.js")
})
$stopped += Stop-ProcessTree $services $processes
Write-Host ("  Backend and frontend: " + $(if ($services.Count) { "stopped" } else { "not running" }))

$processes = @(Get-CimInstance Win32_Process)
$tunnels = @($processes | Where-Object {
    $_.Name -in @("cmd.exe", "ngrok.exe") -and $_.CommandLine -and
    $_.CommandLine -match "\bngrok(?:\.exe)?`"?\s+http\s+(?:https?://)?(?:127\.0\.0\.1:|localhost:)?8000\b"
})
$stopped += Stop-ProcessTree $tunnels $processes
Write-Host ("  Webhook tunnel (ngrok): " + $(if ($tunnels.Count) { "stopped" } else { "not running" }))

if ((Test-Path $pgCtl) -and (Test-Path (Join-Path $pgData "PG_VERSION"))) {
    & $pgCtl -D $pgData status *> $null
    if ($LASTEXITCODE -eq 0) {
        & $pgCtl -D $pgData stop -m fast -w *> $null
        Write-Host ("  PostgreSQL: " + $(if ($LASTEXITCODE -eq 0) { "stopped" } else { "could not stop (see logs\postgresql.log)" }))
    } else {
        Write-Host "  PostgreSQL: not running"
    }
}

$deadline = (Get-Date).AddSeconds(10)
$busy = @()
do {
    $busy = @(8000, 5173, 5432 | Where-Object { Test-PortListening $_ })
    if (-not $busy.Count) { break }
    Start-Sleep -Milliseconds 500
} while ((Get-Date) -lt $deadline)

if ($busy.Count) {
    Write-Warning ("Still listening on port(s): " + ($busy -join ", ") + ". Another program may be using them.")
    exit 1
}
Write-Host "All email checker services are stopped."
